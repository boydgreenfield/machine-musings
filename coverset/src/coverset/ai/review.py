"""Run a :class:`~coverset.ai.client.ReviewClient` over a bundle and shape the findings for storage.

:func:`review_changes` reviews one function per call (``bundle.for_function``), reuses a stored
finding when ``(func_key, bundle_hash)`` matches, skips untrusted bundles unless
``settings.ai_review_untrusted``, and returns :class:`ReviewFindingRow` objects for the orchestrator
to persist in ``review_findings`` (this module never touches the database).

Findings are advisory. :func:`findings_to_invariant_statuses` and :func:`review_evidence` turn them
into invariant input that can only move an invariant out of ``needs_review``: a test or property
outcome (``satisfied``/``violated``) is never changed, and a review never yields ``violated``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from coverset.ai.client import FindingSource, ReviewClient, ReviewError, ReviewOutcome
from coverset.ai.schema import Confidence, FunctionVerdict, ReviewBundle, Verdict
from coverset.logging import redact

if TYPE_CHECKING:
    from coverset.analysis.invariants import ReviewEvidence
    from coverset.config import Settings

__all__ = [
    "PAYLOAD_VERSION",
    "ExistingLookup",
    "ReviewFindingRow",
    "findings_to_invariant_statuses",
    "review_changes",
    "review_evidence",
]

log = logging.getLogger(__name__)

PAYLOAD_VERSION: Final = 1
SOURCE_LABELS: Final[Mapping[str, str]] = {
    "fixture": "fixture",
    "heuristic": "heuristic",
    "anthropic": "Claude (anthropic)",
}
"""How each finding source is labelled in the UI and the PR comment."""
VERDICT_LABELS: Final[Mapping[str, str]] = {
    "covered": "covered",
    "partially_covered": "partially covered",
    "not_covered": "not covered",
    "abstain": "abstained",
}
_REVIEWABLE: Final = frozenset({"needs_review"})


@dataclass(frozen=True, slots=True)
class ReviewFindingRow:
    """One advisory finding, ready for the ``review_findings`` table.

    ``payload`` holds the full :class:`~coverset.ai.schema.FunctionVerdict` under ``"verdict"`` plus
    ``qualname``, ``path``, ``model``, ``usage``, ``cost_usd_estimate`` and ``notes``; the table has
    no columns for model, tokens or cost, so :meth:`to_db_kwargs` keeps them in the payload.
    ``reused`` is True when the row came from ``existing`` instead of a new review.
    """

    func_key: str
    qualname: str
    body_hash: str
    bundle_hash: str
    source: FindingSource
    verdict: Verdict
    confidence: Confidence
    payload: dict[str, Any]
    model: str | None = None
    tokens: int = 0
    cost: float = 0.0
    reused: bool = field(default=False, compare=False)

    @property
    def function_verdict(self) -> FunctionVerdict | None:
        """The stored verdict, re-validated (None when the payload is not a valid verdict)."""
        try:
            return FunctionVerdict.model_validate(self.payload.get("verdict", {}))
        except ValidationError:
            return None

    def to_db_kwargs(self) -> dict[str, Any]:
        """Column values for ``coverset.db.models.ReviewFinding`` (add ``run_id`` yourself)."""
        return {
            "func_key": self.func_key,
            "body_hash": self.body_hash,
            "bundle_hash": self.bundle_hash,
            "source": self.source,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_db(cls, finding: Any) -> ReviewFindingRow:
        """Rebuild a row from a stored ``ReviewFinding`` (or any object with the same attributes)."""
        payload = dict(getattr(finding, "payload", None) or {})
        usage = payload.get("usage") or {}
        return cls(
            func_key=finding.func_key,
            qualname=str(payload.get("qualname", "")),
            body_hash=finding.body_hash,
            bundle_hash=finding.bundle_hash,
            source=finding.source,
            verdict=finding.verdict,
            confidence=finding.confidence,
            payload=payload,
            model=payload.get("model"),
            tokens=int(usage.get("total_tokens", 0) or 0),
            cost=float(payload.get("cost_usd_estimate", 0.0) or 0.0),
            reused=True,
        )


type ExistingLookup = Callable[[str, str], ReviewFindingRow | None]
"""``(func_key, bundle_hash) -> stored row or None``."""


def review_changes(
    client: ReviewClient | None,
    bundle: ReviewBundle,
    *,
    untrusted: bool,
    settings: Settings,
    existing: ExistingLookup | None = None,
) -> list[ReviewFindingRow]:
    """Review every function of ``bundle``, one call per function; return rows to persist.

    Returns ``[]`` when ``client`` is None (AI review off) or when the bundle comes from an untrusted
    PR and ``settings.ai_review_untrusted`` is false. A :class:`ReviewError` for one function is
    logged and that function is skipped (nothing is stored, so the next run retries it).
    """
    if client is None:
        return []
    if untrusted and not settings.ai_review_untrusted:
        log.info("AI review skipped: untrusted PR and COVERSET_AI_REVIEW_UNTRUSTED is false")
        return []
    extra = tuple(settings.secret_values())
    rows: list[ReviewFindingRow] = []
    for fn in bundle.functions:
        sub = bundle.for_function(fn.func_key)
        if existing is not None:
            stored = existing(fn.func_key, sub.bundle_hash)
            if stored is not None:
                rows.append(_as_reused(stored))
                continue
        try:
            outcome = client.review(sub)
        except ReviewError as exc:
            log.warning("AI review of %s failed: %s", fn.func_key, exc)
            continue
        verdict = next((v for v in outcome.result.functions if v.qualname == fn.qualname), None)
        if verdict is None:
            log.warning("AI review of %s returned no verdict for the function", fn.func_key)
            continue
        rows.append(
            _row(fn.func_key, fn.qualname, fn.path, fn.body_hash, sub.bundle_hash, verdict, outcome, extra)
        )
    return rows


def _as_reused(row: ReviewFindingRow) -> ReviewFindingRow:
    if row.reused:
        return row
    return ReviewFindingRow(
        func_key=row.func_key,
        qualname=row.qualname,
        body_hash=row.body_hash,
        bundle_hash=row.bundle_hash,
        source=row.source,
        verdict=row.verdict,
        confidence=row.confidence,
        payload=row.payload,
        model=row.model,
        tokens=row.tokens,
        cost=row.cost,
        reused=True,
    )


def _redact_tree(value: Any, extra: Sequence[str]) -> Any:
    if isinstance(value, str):
        return redact(value, extra)
    if isinstance(value, list):
        return [_redact_tree(v, extra) for v in value]
    if isinstance(value, dict):
        return {k: _redact_tree(v, extra) for k, v in value.items()}
    return value


def _row(
    func_key: str,
    qualname: str,
    path: str,
    body_hash: str,
    bundle_hash: str,
    verdict: FunctionVerdict,
    outcome: ReviewOutcome,
    extra: Sequence[str],
) -> ReviewFindingRow:
    usage = {
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "cache_read_tokens": outcome.cache_read_tokens,
        "cache_write_tokens": outcome.cache_write_tokens,
        "total_tokens": outcome.total_tokens,
        "requests": outcome.requests,
    }
    payload = {
        "payload_version": PAYLOAD_VERSION,
        "advisory": True,
        "qualname": qualname,
        "path": path,
        "source": outcome.source,
        "model": outcome.model,
        "usage": usage,
        "cost_usd_estimate": outcome.cost_usd_estimate,
        "notes": list(outcome.notes),
        "verdict": _redact_tree(verdict.model_dump(mode="json"), extra),
    }
    return ReviewFindingRow(
        func_key=func_key,
        qualname=qualname,
        body_hash=body_hash,
        bundle_hash=bundle_hash,
        source=outcome.source,
        verdict=verdict.verdict,
        confidence=verdict.confidence,
        payload=payload,
        model=outcome.model,
        tokens=outcome.total_tokens,
        cost=outcome.cost_usd_estimate,
    )


# --- invariants (advisory) ---------------------------------------------------------------------


def _reviewer_statuses(findings: Iterable[ReviewFindingRow]) -> dict[str, tuple[str, str, ReviewFindingRow]]:
    """Per invariant id: the combined reviewer status (violated > untested > satisfied), evidence, row.

    Abstained verdicts and ``not_applicable`` findings carry no opinion and are skipped.
    """
    rank = {"satisfied": 0, "untested": 1, "violated": 2}
    combined: dict[str, tuple[str, str, ReviewFindingRow]] = {}
    for row in findings:
        verdict = row.function_verdict
        if verdict is None or verdict.verdict == "abstain":
            continue
        for finding in verdict.invariant_findings:
            if finding.status not in rank:
                continue
            kept = combined.get(finding.invariant_id)
            if kept is None or rank[finding.status] > rank[kept[0]]:
                combined[finding.invariant_id] = (finding.status, finding.evidence, row)
    return combined


def findings_to_invariant_statuses(
    findings: Iterable[ReviewFindingRow], current: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Review-sourced statuses for invariants that are currently ``needs_review``.

    ``current`` maps invariant id → status computed from tests (``coverset.analysis.invariants``);
    only ids whose current status is ``needs_review`` (or that are absent from ``current`` when it
    is None) get an entry. A reviewer ``satisfied`` gives ``satisfied``; anything else keeps
    ``needs_review``, because a review never decides ``violated``. Statuses decided by tests are
    never returned, so callers can ``statuses.update(result)`` safely.
    """
    result: dict[str, str] = {}
    for inv_id, (status, _evidence, _row) in _reviewer_statuses(findings).items():
        if current is not None and current.get(inv_id) not in _REVIEWABLE:
            continue
        result[inv_id] = "satisfied" if status == "satisfied" else "needs_review"
    return result


def review_evidence(findings: Iterable[ReviewFindingRow]) -> dict[str, ReviewEvidence]:
    """Per invariant id, a :class:`~coverset.analysis.invariants.ReviewEvidence` for ``evaluate``.

    Pass it as ``InvariantContext.review_finding``; ``evaluate`` already treats it as advisory (it
    only matters for review-kind verify entries, and only for the same target body hash).
    """
    from coverset.analysis.invariants import ReviewEvidence

    return {
        inv_id: ReviewEvidence(body_hash=row.body_hash, status=status, evidence=evidence, source=row.source)
        for inv_id, (status, evidence, row) in _reviewer_statuses(findings).items()
    }
