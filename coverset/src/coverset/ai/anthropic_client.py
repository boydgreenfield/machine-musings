"""Claude as reviewer, through the Anthropic Python SDK. Implemented per the claude-api skill and
unit-tested against a stubbed SDK only: it has never made a live call (see docs/ai-review.md).

Request shape (one function per request):

* ``client.beta.messages.parse(model="claude-opus-5-5", max_tokens=16000, output_format=ReviewResult,
  output_config={"effort": "medium"}, system=[rubric, repo digest + cache_control], messages=[bundle])``.
  ``thinking`` is omitted (Claude Opus 5.5 always thinks adaptively; disabling it is a 400) and no
  ``temperature`` or assistant prefill is sent (both rejected on this model).
* Server-side refusal fallbacks are enabled: ``betas=["server-side-fallback-2026-07-01"]`` with
  ``fallbacks="default"``, so a classifier decline is retried on Anthropic's recommended fallback
  model inside the same call. The beta messages surface carries both parameters.
* ``stop_reason`` is checked before the parsed output: ``refusal`` (the whole fallback chain
  declined) and ``max_tokens`` become an ``abstain`` verdict with the reason.
* Functions with an ``error``-severity invariant in scope get k=3 self-consistency: three samples
  (tests shuffled differently), missing input classes unioned by name, verdict by majority, and
  confidence from agreement (3/3 high, 2/3 medium, otherwise abstain).
* Rate limits (429), server errors (5xx) and connection errors are retried up to three times with
  exponential backoff (``retry-after`` honoured, capped); the SDK's own retries are turned off so
  attempts are not multiplied. Other API errors raise :class:`~coverset.ai.client.ReviewError`.
* Usage is summed per outcome and priced with the skill's table (Claude Opus 5.5: $4 input, $20
  output, $0.20 cache read, 1.25x input for 5-minute cache writes, per million tokens).

Logging names functions by ``func_key`` and reports token counts and error class names only: never
the bundle, the prompt, the model output or the API key.
"""

from __future__ import annotations

import logging
import random
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

from coverset.ai.client import ConfigError, ReviewError, ReviewOutcome
from coverset.ai.prompts import system_blocks, user_message
from coverset.ai.schema import (
    Evidence,
    FunctionBundle,
    FunctionVerdict,
    InvariantFinding,
    MissingInputClass,
    ReviewBundle,
    ReviewResult,
    SuggestedTest,
)
from coverset.logging import register_secret

if TYPE_CHECKING:
    from coverset.config import Settings

__all__ = [
    "EFFORT",
    "FALLBACKS",
    "FALLBACK_BETA",
    "MAX_TOKENS",
    "MODEL",
    "PRICES",
    "SELF_CONSISTENCY_K",
    "AnthropicReviewClient",
    "Price",
    "combine_samples",
    "estimate_cost",
]

log = logging.getLogger(__name__)

MODEL: Final = "claude-opus-5-5"
EFFORT: Final = "medium"
MAX_TOKENS: Final = 16_000
"""Non-streaming ceiling from the skill; the schema's output plus adaptive thinking fits well inside."""
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
FALLBACKS: Final = "default"
SELF_CONSISTENCY_K: Final = 3
MAX_RETRIES: Final = 3
BACKOFF_BASE_S: Final = 1.0
BACKOFF_CAP_S: Final = 30.0
MAX_EVIDENCE: Final = 5
MAX_SUGGESTED: Final = 3
_SEVERITY_RANK: Final = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


PRICES: Final = {
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20, cache_write=5.00),
    "claude-opus-5": Price(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-8": Price(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
}
"""First-party prices from the claude-api skill (cached 2026-10-06); cache writes are the 5-minute
TTL rate (1.25x input). Fallback models are priced at their own rates."""


def estimate_cost(
    model: str | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float:
    """Estimated USD for one request's usage (unknown models are priced as Claude Opus 5.5)."""
    price = PRICES.get(model or MODEL, PRICES[MODEL])
    cost = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
        + cache_write_tokens * price.cache_write
    ) / 1_000_000
    return round(cost, 6)


@dataclass(slots=True)
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    requests: int = 0
    models: list[str] = field(default_factory=list)

    def add(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        model = getattr(response, "model", None) or MODEL
        tokens = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            "cache_write_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        }
        self.input_tokens += tokens["input_tokens"]
        self.output_tokens += tokens["output_tokens"]
        self.cache_read_tokens += tokens["cache_read_tokens"]
        self.cache_write_tokens += tokens["cache_write_tokens"]
        self.cost += estimate_cost(str(model), **tokens)
        if str(model) not in self.models:
            self.models.append(str(model))


@dataclass(slots=True)
class _Sample:
    verdict: FunctionVerdict | None
    reason: str = ""


class AnthropicReviewClient:
    """A :class:`~coverset.ai.client.ReviewClient` backed by Claude Opus 5.5 (advisory, untested live).

    ``client`` is an ``anthropic.Anthropic`` (or any object with ``beta.messages.parse``); when None,
    one is built from ``settings.anthropic_api_key`` with the SDK's retries disabled.
    """

    name = "anthropic"

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = MAX_RETRIES,
        self_consistency_k: int = SELF_CONSISTENCY_K,
    ) -> None:
        key = settings.anthropic_api_key
        if client is None:
            if key is None or not key.get_secret_value():
                raise ConfigError("COVERSET_AI=anthropic requires ANTHROPIC_API_KEY to be set")
            import anthropic

            client = anthropic.Anthropic(api_key=key.get_secret_value(), max_retries=0)
        if key is not None:
            register_secret(key, min_chars=1)
        self._client = client
        self._sleep = sleep
        self._max_retries = max_retries
        self._k = max(1, self_consistency_k)

    # --- public --------------------------------------------------------------------------------

    def request_kwargs(self, bundle: ReviewBundle, *, salt: int = 0) -> dict[str, Any]:
        """Keyword arguments of one ``beta.messages.parse`` call for ``bundle``."""
        return {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "betas": [FALLBACK_BETA],
            "fallbacks": FALLBACKS,
            "output_config": {"effort": EFFORT},
            "system": system_blocks(bundle.context),
            "messages": [{"role": "user", "content": user_message(bundle, salt=salt)}],
            "output_format": ReviewResult,
        }

    def review(self, bundle: ReviewBundle) -> ReviewOutcome:
        """One request per function (three under self-consistency); refusals become abstentions."""
        usage = _Usage()
        verdicts: list[FunctionVerdict] = []
        notes: list[str] = []
        for fn in bundle.functions:
            sub = bundle.for_function(fn.func_key) if len(bundle.functions) > 1 else bundle
            k = self._k if fn.error_invariants else 1
            samples = [self._sample(sub, fn, salt, usage) for salt in range(k)]
            if k == 1:
                verdicts.append(samples[0].verdict or _abstain(fn, samples[0].reason))
                if samples[0].reason:
                    notes.append(f"{fn.qualname}: {samples[0].reason}")
            else:
                verdict, note = combine_samples(fn, samples)
                verdicts.append(verdict)
                notes.append(f"{fn.qualname}: {note}")
        served = [m for m in usage.models if m != MODEL]
        if served:
            notes.append("served by fallback model " + ", ".join(served))
        return ReviewOutcome(
            result=ReviewResult(functions=verdicts),
            source="anthropic",
            model=", ".join(usage.models) or MODEL,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd_estimate=round(usage.cost, 6),
            requests=usage.requests,
            notes=notes,
        )

    # --- one request ---------------------------------------------------------------------------

    def _sample(self, bundle: ReviewBundle, fn: FunctionBundle, salt: int, usage: _Usage) -> _Sample:
        kwargs = self.request_kwargs(bundle, salt=salt)
        try:
            response = self._call(kwargs, fn.func_key)
        except ValidationError:
            usage.requests += 1
            log.warning("anthropic review of %s: structured output did not validate", fn.func_key)
            return _Sample(None, "The structured output did not validate against the schema.")
        usage.requests += 1
        usage.add(response)
        stop = getattr(response, "stop_reason", None)
        log.info(
            "anthropic review of %s: stop_reason=%s input=%s output=%s cache_read=%s",
            fn.func_key,
            stop,
            getattr(getattr(response, "usage", None), "input_tokens", None),
            getattr(getattr(response, "usage", None), "output_tokens", None),
            getattr(getattr(response, "usage", None), "cache_read_input_tokens", None),
        )
        if stop == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            detail = f" (category: {category})" if category else ""
            return _Sample(None, f"The model declined to review this function{detail}.")
        if stop == "max_tokens":
            return _Sample(None, "The response hit max_tokens before the structured output was complete.")
        parsed = getattr(response, "parsed_output", None)
        if not isinstance(parsed, ReviewResult):
            return _Sample(None, "The response carried no structured output.")
        match = [v for v in parsed.functions if v.qualname == fn.qualname]
        if not match and len(parsed.functions) == 1:
            match = parsed.functions
        if not match:
            return _Sample(None, "The structured output had no verdict for this function.")
        return _Sample(_enforce_rules(match[0], fn))

    def _call(self, kwargs: dict[str, Any], func_key: str) -> Any:
        import anthropic

        error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._client.beta.messages.parse(**kwargs)
            except anthropic.RateLimitError as exc:
                error = exc
                retry_after = _retry_after(exc)
            except anthropic.APIStatusError as exc:
                if exc.status_code < 500:
                    raise ReviewError(
                        f"Anthropic API rejected the review request: HTTP {exc.status_code} ({type(exc).__name__})"
                    ) from exc
                error, retry_after = exc, _retry_after(exc)
            except anthropic.APIConnectionError as exc:
                error, retry_after = exc, None
            if attempt == self._max_retries:
                break
            delay = retry_after if retry_after is not None else BACKOFF_BASE_S * 2**attempt
            delay = min(BACKOFF_CAP_S, delay) + random.uniform(0, 0.25)
            log.warning(
                "anthropic review of %s: %s, retry %d/%d in %.1fs",
                func_key,
                type(error).__name__,
                attempt + 1,
                self._max_retries,
                delay,
            )
            self._sleep(delay)
        name = type(error).__name__ if error else "unknown error"
        raise ReviewError(f"Anthropic API failed after {self._max_retries + 1} attempts ({name})") from error


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    value = headers.get("retry-after") if headers is not None else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --- verdict hygiene and self-consistency --------------------------------------------------------


def _abstain(fn: FunctionBundle, reason: str) -> FunctionVerdict:
    return FunctionVerdict(
        qualname=fn.qualname,
        verdict="abstain",
        confidence="low",
        evidence=[],
        missing_input_classes=[],
        suggested_tests=[],
        invariant_findings=[],
        abstain_reason=reason or "The reviewer returned no verdict.",
    )


def _known_tests(fn: FunctionBundle) -> set[str]:
    known = {t.nodeid for t in fn.covering_tests}
    known |= {t for s in fn.observed_shapes for t in s.tests}
    known |= {t for inv in fn.invariants for t in inv.linked_tests}
    return known


def _enforce_rules(verdict: FunctionVerdict, fn: FunctionBundle) -> FunctionVerdict:
    """Apply the output rules the schema cannot express (exact qualname, cited tests exist, ...)."""
    known = _known_tests(fn)
    inv_ids = {inv.id for inv in fn.invariants}
    data = verdict.model_copy(deep=True)
    data.qualname = fn.qualname
    data.evidence = [e for e in data.evidence if e.test_id in known][:MAX_EVIDENCE]
    data.suggested_tests = data.suggested_tests[:MAX_SUGGESTED]
    data.invariant_findings = [f for f in data.invariant_findings if f.invariant_id in inv_ids]
    if data.verdict == "not_covered" and not data.missing_input_classes:
        return _abstain(fn, "The reviewer said not_covered without naming a missing input class.")
    if data.verdict == "abstain":
        data.abstain_reason = data.abstain_reason.strip() or "The reviewer abstained without a reason."
    else:
        data.abstain_reason = ""
    return data


def combine_samples(fn: FunctionBundle, samples: Sequence[_Sample]) -> tuple[FunctionVerdict, str]:
    """Merge k samples: majority verdict, agreement-based confidence, unions by name.

    Every sample votes (a failed or refused sample votes ``abstain``). 3 of 3 agreeing gives high
    confidence, 2 of 3 medium; no majority, or a majority for ``abstain``, gives an abstention.
    """
    k = len(samples)
    votes = Counter(s.verdict.verdict if s.verdict else "abstain" for s in samples)
    (top, count), *_ = votes.most_common(1) or [("abstain", 0)]
    tied = sum(1 for c in votes.values() if c == count) > 1
    tally = ", ".join(f"{v} {c}" for v, c in sorted(votes.items()))
    note = f"self-consistency k={k}: {tally}"
    valid = [s.verdict for s in samples if s.verdict is not None and s.verdict.verdict != "abstain"]
    if tied or count * 2 <= k or top == "abstain":
        reasons = sorted(
            {s.reason or (s.verdict.abstain_reason if s.verdict else "") for s in samples} - {""}
        )
        why = "; ".join(reasons) if top == "abstain" and reasons else f"samples disagreed ({tally})"
        verdict = _abstain(fn, f"Self-consistency abstained: {why}.")
        verdict.missing_input_classes = _union_missing(valid)
        return verdict, note
    agreeing = [v for v in valid if v.verdict == top]
    confidence = "high" if count == k else "medium"
    merged = FunctionVerdict(
        qualname=fn.qualname,
        verdict=top,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        evidence=_union_evidence(agreeing),
        missing_input_classes=_union_missing(valid),
        suggested_tests=_union_tests(agreeing),
        invariant_findings=_majority_findings(valid),
        abstain_reason="",
    )
    if merged.verdict == "not_covered" and not merged.missing_input_classes:
        return _abstain(fn, "The reviewer said not_covered without naming a missing input class."), note
    return merged, note


def _union_missing(verdicts: Sequence[FunctionVerdict]) -> list[MissingInputClass]:
    by_name: dict[str, MissingInputClass] = {}
    for verdict in verdicts:
        for cls in verdict.missing_input_classes:
            kept = by_name.get(cls.name)
            if kept is None:
                by_name[cls.name] = cls.model_copy()
            elif _SEVERITY_RANK[cls.severity] > _SEVERITY_RANK[kept.severity]:
                kept.severity = cls.severity
    return sorted(by_name.values(), key=lambda c: -_SEVERITY_RANK[c.severity])


def _union_evidence(verdicts: Sequence[FunctionVerdict]) -> list[Evidence]:
    seen: dict[str, Evidence] = {}
    for verdict in verdicts:
        for item in verdict.evidence:
            seen.setdefault(item.test_id, item)
    return list(seen.values())[:MAX_EVIDENCE]


def _union_tests(verdicts: Sequence[FunctionVerdict]) -> list[SuggestedTest]:
    seen: dict[str, SuggestedTest] = {}
    for verdict in verdicts:
        for test in verdict.suggested_tests:
            seen.setdefault(test.name, test)
    return list(seen.values())[:MAX_SUGGESTED]


def _majority_findings(verdicts: Sequence[FunctionVerdict]) -> list[InvariantFinding]:
    grouped: dict[str, list[InvariantFinding]] = {}
    for verdict in verdicts:
        for finding in verdict.invariant_findings:
            grouped.setdefault(finding.invariant_id, []).append(finding)
    merged: list[InvariantFinding] = []
    for inv_id, findings in grouped.items():
        counts = Counter(f.status for f in findings)
        status, count = counts.most_common(1)[0]
        if count * 2 > len(verdicts):
            evidence = next(f.evidence for f in findings if f.status == status)
            merged.append(InvariantFinding(invariant_id=inv_id, status=status, evidence=evidence))
        else:
            tally = ", ".join(f"{s} {c}" for s, c in sorted(counts.items()))
            merged.append(
                InvariantFinding(
                    invariant_id=inv_id, status="untested", evidence=f"Samples disagreed ({tally})."
                )
            )
    return merged
