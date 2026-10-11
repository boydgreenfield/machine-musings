"""From a finished (or first-failing) run to its :class:`VerdictSummary`, stored and posted.

:func:`build_report` gathers what the summary shows beyond pass/fail, once the shard bundles are
ingested: patch and changed-function coverage (:func:`coverset.analysis.patch.patch_report` over the
run's facts), input-class coverage of the changed functions and of every invariant's target
(classes missing with ``missing_reason == "never exercised"`` are listed, each with a Hypothesis
snippet), invariant statuses (head definitions plus base definitions the PR removed, each with its
coverage lens: whether the linked tests ran the target's changed lines comes from the run's facts),
plus the base definition of every invariant the PR weakened or removed (from
:func:`~coverset.analysis.invariants.diff_invariants`), the PR's changes to ``invariants.yaml``, and
the optional AI review. CPU-heavy pieces (shape catalogues, test bodies) run in threads.

:func:`build_summary` adds the run-level numbers: the first confirmed failure's ``rank_coverset``
(its planned position in the global priority order; ``runs.rank_executed`` keeps the position at
which its first attempt actually reported), ``rank_file_order`` (its position in plain collection
order) and ``t_first_failure_file_order_est = (first_test - t0) + Σ p50 of the tests before it in
file order`` (an estimate, labelled "estimated from the first test"), where ``t0`` is when the
webhook arrived or else when the run was queued. :func:`persist_verdict` writes ``runs.verdict/reason/summary_json`` and
:func:`post_summary` sends it through the forge when the run belongs to a PR.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.analysis.diff import is_behaviour_preserving
from coverset.analysis.invariants import (
    Invariant,
    InvariantContext,
    InvariantError,
    PreviousResult,
    ReviewEvidence,
    diff_invariants,
    evaluate,
    inv_key,
    invariant_lens,
    load_invariants,
    resolve_target,
    spec_hash,
)
from coverset.analysis.patch import PatchReport, patch_report
from coverset.analysis.shapes import NEVER_EXERCISED, Catalogue, FunctionInputCov, function_catalogue
from coverset.analysis.sharding import UNKNOWN_DURATION_S
from coverset.config import Settings
from coverset.db.models import Invariant as InvariantModel
from coverset.db.models import InvariantResult as InvariantResultModel
from coverset.db.models import PullRequest, Repo, ReviewFinding, Run
from coverset.facts.queries import tests_covering
from coverset.facts.store import FactsStore
from coverset.forge.verdict import (
    FailureRow,
    FunctionRow,
    InvariantRow,
    MissingClass,
    VerdictSummary,
)
from coverset.lang.base import FunctionChange, FunctionRegion, ShapeObs, StaticFile
from coverset.orchestrator.bundles import IngestOutcome, merge_sampling
from coverset.orchestrator.repos import read_blob

if TYPE_CHECKING:
    from coverset.orchestrator.planner import RunPlan

__all__ = [
    "RunReport",
    "TestState",
    "build_report",
    "ESTIMATE_LABEL",
    "build_summary",
    "file_order_estimate",
    "functions_of_interest",
    "outcome_tally",
    "source_changes",
    "persist_verdict",
    "post_summary",
    "repo_ref",
    "summary_from_json",
    "summary_to_json",
    "t0_of",
]

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 300


@dataclass(slots=True)
class TestState:
    """The runner's view of one test (final, after retries and reconciliation)."""

    __test__ = False

    nodeid: str
    outcome: str = "not_run"
    duration_s: float | None = None
    failed_phase: str | None = None
    fail_sig: str | None = None
    longrepr: str | None = None
    shard_id: int | None = None
    attempts: int = 0
    flaky: bool = False
    confirmed: bool = False
    preexisting: bool = False
    exec_index: int | None = None
    """How many first-attempt results (all shards) had arrived when this one did (1-based)."""


@dataclass(slots=True)
class RunReport:
    """Everything :func:`build_summary` adds beyond test outcomes."""

    patch: PatchReport | None = None
    function_rows: tuple[FunctionRow, ...] = ()
    missing: tuple[MissingClass, ...] = ()
    invariants: tuple[InvariantRow, ...] = ()
    invariants_changed: tuple[InvariantRow, ...] = ()
    ai_source: str | None = None
    input_cov: dict[str, FunctionInputCov] = field(default_factory=dict)
    """Input-class coverage by ``func_key`` (changed functions and invariant targets)."""
    observations: dict[str, list[ShapeObs]] = field(default_factory=dict)
    """Shape observations by ``func_key`` (the same functions)."""
    notes: list[str] = field(default_factory=list)


def t0_of(run: Run) -> datetime:
    """When the run's clock starts: webhook arrival if recorded, else when it was queued/created."""
    for key in ("webhook_received", "queued"):
        value = (run.timeline or {}).get(key)
        if value:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                continue
    return run.created_at


def source_changes(plan: RunPlan) -> list[FunctionChange]:
    """The plan's function changes in measured source files (test files are not reported on)."""
    measured = plan.measured_sources
    return [
        c
        for c in plan.changes
        if any(side is not None and side.path in measured for side in (c.region, c.prev))
        or (c.region is None and c.prev is not None and c.prev.path.startswith(_source_prefix(plan)))
    ]


def _source_prefix(plan: RunPlan) -> str:
    root = plan.config.source_root
    return "" if root in ("", ".") else root.rstrip("/") + "/"


def functions_of_interest(plan: RunPlan) -> dict[str, FunctionRegion]:
    """Changed (non-deleted) source functions and methods plus invariant targets, by ``func_key``."""
    found: dict[str, FunctionRegion] = {}
    for change in source_changes(plan):
        region = change.region
        if region is not None and change.kind != "deleted" and region.kind in ("function", "method"):
            found[region.func_key] = region
    for region in plan.invariant_targets.values():
        found[region.func_key] = region
    return found


ESTIMATE_LABEL = "estimated from the first test"
"""How every reader labels ``t_first_failure_file_order_est``."""


def file_order_estimate(plan: RunPlan, nodeid: str, first_test_s: float) -> float | None:
    """``first_test_s + Σ p50`` of the tests before ``nodeid`` in collection order (estimated).

    ``first_test_s`` is ``timeline.first_test - t0``: everything before the first test (the
    environment, collection, planning) happens whichever order the tests run in, so the estimate
    starts where the orders begin to differ. Unknown p50s count as the median of the known ones,
    or as :data:`~coverset.analysis.sharding.UNKNOWN_DURATION_S` (what the planner assumes) when no
    test has been timed.
    """
    ranks = plan.file_ranks
    if nodeid not in ranks:
        return None
    known = [d for d in plan.durations.values() if d > 0]
    median = statistics.median(known) if known else UNKNOWN_DURATION_S
    before = plan.collected[: ranks[nodeid] - 1]
    return round(first_test_s + sum(plan.durations.get(i.nodeid, median) for i in before), 3)


# -- the report ---------------------------------------------------------------------------------


async def build_report(
    session: AsyncSession,
    settings: Settings,
    store: FactsStore,
    run: Run,
    plan: RunPlan,
    outcomes: Sequence[IngestOutcome],
    states: Mapping[str, TestState],
    *,
    catalogue: Catalogue,
) -> RunReport:
    """Patch, function, input-class, invariant and AI facts of a run whose bundles are ingested."""
    report = RunReport()
    ok = [o for o in outcomes if o.ok]
    statics: dict[str, StaticFile] = {}
    union: dict[str, set[int]] = defaultdict(set)
    for outcome in ok:
        statics.update(outcome.statics)
        for path, lines in outcome.union_lines.items():
            union[path] |= lines
    if plan.changed_lines and ok:

        def covering(path: str, start: int, end: int) -> list[str]:
            return tests_covering(store, run.repo_id, run.id, path, start, end)

        report.patch = await asyncio.to_thread(
            patch_report, plan.changed_lines, statics, dict(union), source_changes(plan), covering
        )
    interest = functions_of_interest(plan)

    def called(region: FunctionRegion) -> list[str]:
        return tests_covering(store, run.repo_id, run.id, region.path, region.body_start, region.end_line)

    report.input_cov, report.observations = await asyncio.to_thread(
        _input_coverage, interest, ok, plan.measured_sources, called if ok else None
    )
    report.ai_source, evidence = await _ai_review(session, settings, run, plan, report)
    invariant_rows, by_target = await _invariants(
        session, run, plan, states, report.input_cov, evidence, store=store, statics=statics
    )
    report.invariants = tuple(invariant_rows)
    report.invariants_changed = tuple(await _invariant_changes(plan, run))
    report.function_rows = tuple(_function_rows(report.patch, report.input_cov, by_target))
    report.missing = tuple(_missing_classes(interest, report.input_cov))
    return report


def _input_coverage(
    interest: Mapping[str, FunctionRegion],
    outcomes: Sequence[IngestOutcome],
    sources: Mapping[str, str],
    called: Callable[[FunctionRegion], list[str]] | None = None,
) -> tuple[dict[str, FunctionInputCov], dict[str, list[ShapeObs]]]:
    """Input-class coverage and the shape observations of each function of interest, by ``func_key``.

    Each function's catalogue comes from :func:`~coverset.analysis.shapes.function_catalogue` over
    its head source (``sources``), the helper the PR page's cards use, so the comment's "N input
    classes never exercised" and the cards count the same classes. ``called(region)`` lists the
    tests whose lines touch the body; it is asked only for a function whose sampling was capped,
    so "K tests hit the sample cap" counts what the UI counts.
    """
    by_triple: dict[tuple[str, str, int], list[ShapeObs]] = defaultdict(list)
    for outcome in outcomes:
        for obs in outcome.shapes:
            by_triple[obs.func].append(obs)
    sampling = merge_sampling([s for o in outcomes for s in o.sampling])
    covs: dict[str, FunctionInputCov] = {}
    observations: dict[str, list[ShapeObs]] = {}
    for key, region in interest.items():
        observations[key] = by_triple.get(region.triple, [])
        row = sampling.get(region.triple)
        capped = (row is not None and row.truncated) or any(o.truncated for o in observations[key])
        tests_called = None
        if capped and called is not None:
            try:
                tests_called = called(region)
            except Exception as exc:  # facts unreadable: the count falls back to its floor
                log.warning("cannot read the callers of %s: %s", region.qualname, exc)
        found = function_catalogue(region, sources.get(region.path))
        covs[key] = found.input_cov(region, observations[key], row, tests_called=tests_called)
    return covs, observations


def _missing_classes(
    interest: Mapping[str, FunctionRegion], input_cov: Mapping[str, FunctionInputCov]
) -> list[MissingClass]:
    """Never-exercised classes of functions some test called (hint = Hypothesis snippet)."""
    rows: list[MissingClass] = []
    for key, region in sorted(interest.items(), key=lambda kv: (kv[1].path, kv[1].def_line)):
        cov = input_cov.get(key)
        if cov is None or not cov.observed or cov.missing_reason != NEVER_EXERCISED:
            continue
        for cls in cov.missing:
            rows.append(MissingClass(region.qualname, cls.param, cls.name, cls.hypothesis_hint))
    return rows


def _function_rows(
    patch: PatchReport | None,
    input_cov: Mapping[str, FunctionInputCov],
    invariants_by_target: Mapping[str, list[InvariantRow]],
) -> list[FunctionRow]:
    if patch is None:
        return []
    rows = []
    for row in patch.function_rows:
        region = row.change.region
        assert region is not None
        cov = input_cov.get(region.func_key)
        missing = tuple(c.name for c in cov.missing) if cov is not None and cov.observed else ()
        rows.append(
            FunctionRow(
                qualname=region.qualname,
                change_kind=row.change.kind,
                tests=len(row.tests),
                line_pct=row.line_pct,
                input_classes_missing=missing,
                invariants=tuple(invariants_by_target.get(region.func_key, ())),
                inherited=row.inherited or is_behaviour_preserving(row.change),
            )
        )
    return rows


# -- invariants ---------------------------------------------------------------------------------


def _load(text: bytes | None, label: str) -> list[Invariant]:
    """Parse an ``invariants.yaml``; a missing file means none, an invalid one is logged and ignored."""
    if text is None:
        return []
    try:
        return load_invariants(text.decode("utf-8"))
    except (InvariantError, UnicodeDecodeError) as exc:
        log.warning("%s invariants ignored: %s", label, exc)
        return []


def _head_invariants(plan: RunPlan) -> list[Invariant]:
    path = plan.checkout / plan.config.invariants
    text = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    return _load(text, "head")


async def _base_invariants(plan: RunPlan) -> list[Invariant]:
    if plan.merge_base is None:
        return []
    blob = await asyncio.to_thread(read_blob, plan.mirror, plan.merge_base, plan.config.invariants)
    return _load(blob, "base")


async def _invariant_changes(plan: RunPlan, run: Run) -> list[InvariantRow]:
    if run.base_sha is None:
        return []
    changes = diff_invariants(await _base_invariants(plan), await asyncio.to_thread(_head_invariants, plan))
    return [
        InvariantRow(change.inv_id, change.kind, "; ".join(change.reasons) or None)
        for change in changes
        if change.kind != "unchanged"
    ]


async def _invariants(
    session: AsyncSession,
    run: Run,
    plan: RunPlan,
    states: Mapping[str, TestState],
    input_cov: Mapping[str, FunctionInputCov],
    review: Mapping[str, ReviewEvidence],
    *,
    store: FactsStore | None = None,
    statics: Mapping[str, StaticFile] | None = None,
) -> tuple[list[InvariantRow], dict[str, list[InvariantRow]]]:
    """Evaluate head invariants plus the base definitions the PR weakened or removed, persist them,
    return summary rows (a base definition's row reads ``<id> (base)``)."""
    head = await asyncio.to_thread(_head_invariants, plan)
    base = await _base_invariants(plan)
    changes = diff_invariants(base, head) if base else []
    base_specs = [
        (c.base, c.kind) for c in changes if c.base is not None and c.kind in ("weakened", "removed")
    ]
    results = {nodeid: state.outcome for nodeid, state in states.items()}
    links: dict[str, tuple[str, ...]] = {i.nodeid: i.invariant_ids for i in plan.collected if i.invariant_ids}
    hypothesis = {i.nodeid: i.hypothesis for i in plan.collected}
    rows: list[InvariantRow] = []
    by_target: dict[str, list[InvariantRow]] = defaultdict(list)
    evaluations: list[tuple[Invariant, str | None]] = [(inv, None) for inv in head]
    evaluations += [(inv, kind) for inv, kind in base_specs]
    for inv, base_kind in evaluations:
        region = resolve_target(inv, plan.head_functions, plan.config.source_root)
        model = await _invariant_row(session, run, inv)
        previous = await _previous_result(session, run, inv)
        finding = review.get(inv.id) or await _stored_review_evidence(session, inv, region)
        result = evaluate(
            inv,
            InvariantContext(
                results=results,
                links=links,
                hypothesis=hypothesis,
                target_region=region,
                previous_result=previous,
                review_finding=finding,
            ),
        )
        linked = [
            n
            for n, ids in links.items()
            if inv.id in ids or any(v.marker in ids for v in inv.verify if v.marker)
        ]
        cov = input_cov.get(region.func_key) if region is not None else None
        covers = await _covers_changed_lines(store, run, plan, region, linked, statics=statics)
        lens = invariant_lens(inv, cov, linked, covers_changed_lines=covers)
        evidence = {**result.evidence, "lens": lens.summary}
        if base_kind is not None:
            evidence["base_definition"] = True
            evidence[f"{base_kind}_in_head"] = True
        session.add(
            InvariantResultModel(
                run_id=run.id,
                invariant_id=model.id,
                status=result.status,
                trigger="run",
                target_func_key=region.func_key if region is not None else None,
                target_body_hash=region.body_hash if region is not None else None,
                covering_test_ids=[plan.test_ids[n] for n in result.covering_test_ids if n in plan.test_ids],
                evidence=evidence,
            )
        )
        label = inv.id if base_kind is None or base_kind == "removed" else f"{inv.id} (base)"
        row = InvariantRow(label, result.status, lens.summary)
        rows.append(row)
        if region is not None:
            by_target[region.func_key].append(row)
    await session.flush()
    return rows, by_target


async def _covers_changed_lines(
    store: FactsStore | None,
    run: Run,
    plan: RunPlan,
    region: FunctionRegion | None,
    linked: Sequence[str],
    *,
    statics: Mapping[str, StaticFile] | None = None,
) -> bool | None:
    """Whether a linked test executed one of the target's changed lines (None: target unchanged).

    Read from the run's facts (``test_lines``), with the body-line rule of
    :mod:`coverset.analysis.patch`: only changed *body* statements (``[body_start, end_line]``, and
    statements when the file's static facts are known) are asked about, because test contexts never
    hold a ``def``, decorator or docstring line (import time runs those). A change that touches no
    body statement (a signature-, decorator- or docstring-only change, or only deletions) asks about
    the whole body span: did a linked test call the function at all.
    """
    if region is None or not any(
        c.region is not None and c.region.func_key == region.func_key for c in plan.changes
    ):
        return None
    if not linked:
        return False
    if store is None:
        return None
    lines = {n for n in plan.changed_lines.get(region.path, ()) if region.body_start <= n <= region.end_line}
    static = statics.get(region.path) if statics else None
    if static is not None:
        lines &= set(static.statements) - set(static.excluded)
    spans = _spans(sorted(lines)) if lines else [(region.body_start, region.end_line)]
    wanted = set(linked)

    def query() -> bool:
        for start, end in spans:
            if wanted.intersection(tests_covering(store, run.repo_id, run.id, region.path, start, end)):
                return True
        return False

    try:
        return await asyncio.to_thread(query)
    except Exception as exc:  # facts unavailable (partial ingest): unknown, not "no"
        log.warning("cannot read covering tests of %s: %s", region.qualname, exc)
        return None


def _spans(lines: Sequence[int]) -> list[tuple[int, int]]:
    """Sorted line numbers as inclusive ``(start, end)`` runs."""
    spans: list[tuple[int, int]] = []
    for line in lines:
        if spans and line == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], line)
        else:
            spans.append((line, line))
    return spans


async def _invariant_row(session: AsyncSession, run: Run, inv: Invariant) -> InvariantModel:
    digest = spec_hash(inv)
    model = await session.scalar(
        select(InvariantModel).where(
            InvariantModel.repo_id == run.repo_id,
            InvariantModel.inv_key == inv_key(inv),
            InvariantModel.spec_hash == digest,
        )
    )
    if model is None:
        model = InvariantModel(
            repo_id=run.repo_id,
            inv_key=inv_key(inv),
            spec_hash=digest,
            spec_json=dict(inv.spec),
            first_seen_sha=run.head_sha,
            last_seen_sha=run.head_sha,
        )
        session.add(model)
        await session.flush()
    else:
        model.last_seen_sha = run.head_sha
    return model


async def _previous_result(session: AsyncSession, run: Run, inv: Invariant) -> PreviousResult | None:
    """The latest earlier result of ``inv`` that is real evidence (``stale`` rows are derived and
    skipped, so staleness persists until fresh evidence arrives); within one run, the row of the
    same spec wins over a PR's ``(base)`` row."""
    row = (
        await session.execute(
            select(InvariantResultModel, InvariantModel.spec_hash)
            .join(InvariantModel, InvariantModel.id == InvariantResultModel.invariant_id)
            .join(Run, Run.id == InvariantResultModel.run_id)
            .where(
                InvariantModel.repo_id == run.repo_id,
                InvariantModel.inv_key == inv_key(inv),
                InvariantResultModel.run_id != run.id,
                InvariantResultModel.status != "stale",
                Run.created_at <= run.created_at,
            )
            .order_by(Run.created_at.desc(), (InvariantModel.spec_hash == spec_hash(inv)).desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    result, digest = row
    return PreviousResult(result.run_id, result.status, result.target_body_hash, digest)


async def _stored_review_evidence(
    session: AsyncSession, inv: Invariant, region: FunctionRegion | None
) -> ReviewEvidence | None:
    """Review evidence about ``inv`` from earlier findings for the target's current body, if any."""
    if region is None or not any(v.kind == "review" for v in inv.verify):
        return None
    try:
        from coverset.ai.review import ReviewFindingRow, review_evidence
    except ImportError:
        return None
    findings = await session.scalars(
        select(ReviewFinding)
        .where(ReviewFinding.func_key == region.func_key, ReviewFinding.body_hash == region.body_hash)
        .order_by(ReviewFinding.created_at.desc())
        .limit(10)
    )
    rows = [ReviewFindingRow.from_db(f) for f in findings]
    return review_evidence(rows).get(inv.id) if rows else None


# -- AI review (phase 7) --------------------------------------------------------------------------


async def _ai_review(
    session: AsyncSession, settings: Settings, run: Run, plan: RunPlan, report: RunReport
) -> tuple[str | None, dict[str, ReviewEvidence]]:
    """Run the advisory AI review of the changed functions; store new findings.

    Returns the ``ai_source`` label (``off``, ``unavailable``, ``skipped``, or the sources of the
    findings such as ``fixture``/``heuristic``/``anthropic``) and review evidence per invariant id.
    A missing or failing reviewer never fails the run.
    """
    if settings.ai == "off":
        return "off", {}
    if not run.trusted and not settings.ai_review_untrusted:
        return "skipped", {}
    try:
        from coverset.ai.bundle import build_review_bundle
        from coverset.ai.client import get_review_client
        from coverset.ai.review import ReviewFindingRow, review_changes, review_evidence
    except ImportError:
        return "unavailable", {}
    changed = [
        c for c in source_changes(plan) if c.region is not None and c.region.kind in ("function", "method")
    ]
    if not changed:
        return None, {}
    try:
        repo = await session.get(Repo, run.repo_id)
        client = get_review_client(settings, repo_enabled=bool(repo and repo.ai_review_enabled))
        if client is None:
            return "off", {}
        keys = [c.region.func_key for c in changed if c.region is not None]
        stored = await session.scalars(select(ReviewFinding).where(ReviewFinding.func_key.in_(keys)))
        existing = {(f.func_key, f.bundle_hash): ReviewFindingRow.from_db(f) for f in stored}
        bundle = build_review_bundle(
            repo_name=repo.full_name if repo else str(run.repo_id),
            head_sha=run.head_sha,
            pr_number=None,
            changes=changed,
            sources=plan.measured_sources,
            diff_hunks=plan.diff_text,
            patch_rows=report.patch.function_rows if report.patch is not None else None,
            covering_tests=await asyncio.to_thread(_covering_tests, plan, report),
            input_covs=report.input_cov,
            shapes=report.observations,
            invariants=await asyncio.to_thread(_head_invariants, plan),
            settings=settings,
            invariant_links=_invariant_links(plan),
            source_root=plan.config.source_root,
            language="python",
        )
        rows = await asyncio.to_thread(
            review_changes,
            client,
            bundle,
            untrusted=not run.trusted,
            settings=settings,
            existing=lambda key, digest: existing.get((key, digest)),
        )
    except Exception as exc:  # advisory: a broken reviewer never fails the run
        log.warning("AI review failed for run %s: %s", run.id, exc)
        return "unavailable", {}
    now = settings.now()
    for row in rows:
        if not row.reused:
            session.add(ReviewFinding(run_id=run.id, created_at=now, **row.to_db_kwargs()))
    await session.flush()
    sources = sorted({str(row.source) for row in rows})
    return (", ".join(sources) if sources else None), review_evidence(rows)


def _covering_tests(plan: RunPlan, report: RunReport) -> dict[str, list[tuple[str, str]]]:
    """Per changed function, its covering tests in priority order with their bodies."""
    if report.patch is None:
        return {}
    ranks = plan.ranks
    bodies: dict[str, str] = {}
    result: dict[str, list[tuple[str, str]]] = {}
    for row in report.patch.function_rows:
        region = row.change.region
        if region is None:
            continue
        tests = sorted(row.tests, key=lambda n: ranks.get(n, 10**9))[:5]
        result[region.func_key] = [(n, bodies.setdefault(n, _test_body(plan, n))) for n in tests]
    return result


def _test_body(plan: RunPlan, nodeid: str) -> str:
    """Source of the test function behind ``nodeid`` in the head checkout ('' when not found)."""
    path, _, rest = nodeid.partition("::")
    qualname = rest.split("[", 1)[0].replace("::", ".")
    file = plan.checkout / path
    if not rest or not file.is_file() or file.is_symlink():
        return ""
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    from coverset.analysis.functions import index_functions

    for region in index_functions(path, source):
        if region.qualname == qualname:
            lines = source.splitlines()
            return "\n".join(lines[region.def_line - 1 : region.end_line])
    return ""


def _invariant_links(plan: RunPlan) -> dict[str, list[str]]:
    """Invariant id (or marker) → nodeids linked by ``@pytest.mark.invariant``."""
    links: dict[str, list[str]] = defaultdict(list)
    for item in plan.collected:
        for inv_id in item.invariant_ids:
            links[inv_id].append(item.nodeid)
    return dict(links)


# -- summary ------------------------------------------------------------------------------------


def build_summary(
    settings: Settings,
    run: Run,
    plan: RunPlan | None,
    states: Mapping[str, TestState],
    *,
    conclusion: str,
    reason: str,
    report: RunReport | None = None,
) -> VerdictSummary:
    """The :class:`VerdictSummary` of a run (``report`` is None for the early first-failure verdict)."""
    failures = [s for s in states.values() if s.confirmed and not s.preexisting]
    items = plan.items_by_nodeid if plan is not None else {}
    collect_errors = getattr(plan, "collect_errors", None) or []
    failure_rows = tuple(
        FailureRow(
            nodeid=s.nodeid,
            message=_message(s.longrepr),
            path=items[s.nodeid].file if s.nodeid in items else None,
            line=items[s.nodeid].line if s.nodeid in items else None,
        )
        for s in failures
    ) + tuple(_collection_error_row(error) for error in collect_errors)
    first_failure_s = None
    first = (run.timeline or {}).get("first_failure")
    if first:
        first_failure_s = round((datetime.fromisoformat(first) - t0_of(run)).total_seconds(), 3)
    patch = report.patch if report is not None else None
    title = _title(conclusion, reason, failure_rows, len(collect_errors), states, patch)
    return VerdictSummary(
        conclusion=conclusion,  # type: ignore[arg-type]
        title=title,
        reason=reason,
        first_failure_s=first_failure_s,
        rank_coverset=run.rank_coverset,
        rank_file_order=run.rank_file_order,
        t_first_failure_file_order_est=run.t_first_failure_file_order_est,
        patch_pct=patch.patch_pct if patch is not None else None,
        patch_covered=patch.covered if patch is not None else 0,
        patch_total=patch.total if patch is not None else 0,
        function_rows=report.function_rows if report is not None else (),
        zero_test_functions=len(patch.zero_test_functions) if patch is not None else 0,
        missing_input_classes=report.missing if report is not None else (),
        invariants=report.invariants if report is not None else (),
        invariants_changed=report.invariants_changed if report is not None else (),
        failures=failure_rows,
        details_url=f"{settings.public_url}/runs/{run.id}",
        ai_source=report.ai_source if report is not None else None,
        tests_total=len(plan.collected) if plan is not None else None,
    )


def _title(
    conclusion: str,
    reason: str,
    failures: Sequence[FailureRow],
    collect_errors: int,
    states: Mapping[str, TestState],
    patch: PatchReport | None,
) -> str:
    if conclusion == "failure" and failures:
        more = f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
        verb = "failed to collect" if len(failures) == collect_errors else "failed"
        return f"{failures[0].nodeid} {verb}{more}"[:255]
    if conclusion == "success":
        text = outcome_tally(states)
        if all(t.outcome in ("passed", "not_run") for t in states.values()):
            text = f"{sum(1 for t in states.values() if t.outcome == 'passed')} tests passed"
        if patch is not None and patch.patch_pct is not None:
            text += f" · patch coverage {patch.patch_pct:g}%"
        return text[:255]
    return (reason or conclusion)[:255]


def outcome_tally(states: Mapping[str, TestState]) -> str:
    """``N passed, 1 pre-existing failure, K skipped``: the outcomes of a run that did not fail.

    Only passed tests count as passed; pre-existing failures, quarantined failures, skips, xfails
    and xpasses are named when present.
    """
    counts: dict[str, int] = defaultdict(int)
    for test in states.values():
        counts[test.outcome] += 1
    preexisting = sum(1 for t in states.values() if t.preexisting)
    other_failed = counts["failed"] + counts["error"] - preexisting
    parts = [f"{counts['passed']} passed"]
    if preexisting:
        parts.append(f"{preexisting} pre-existing failure{'' if preexisting == 1 else 's'}")
    if other_failed > 0:
        parts.append(f"{other_failed} quarantined failure{'' if other_failed == 1 else 's'}")
    for outcome in ("skipped", "xfailed", "xpassed"):
        if counts[outcome]:
            parts.append(f"{counts[outcome]} {outcome}")
    return ", ".join(parts)


def _collection_error_row(error: Mapping[str, Any]) -> FailureRow:
    """A test module that failed to import, as a failure row (its node id is the module path)."""
    nodeid = str(error.get("nodeid") or "<collection>")
    longrepr = error.get("longrepr")
    return FailureRow(
        nodeid=nodeid,
        message=_message(longrepr if isinstance(longrepr, str) else None, default="collection error"),
        path=nodeid.split("::", 1)[0] if nodeid.endswith(".py") or "::" in nodeid else None,
        line=None,
    )


def _message(longrepr: str | None, *, default: str = "test failed") -> str:
    if not longrepr or not longrepr.strip():
        return default
    lines = [line.strip() for line in longrepr.strip().splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("E ")]
    text = errors[0][1:].strip() if errors else lines[-1]
    return text[:MAX_MESSAGE_CHARS]


def summary_to_json(summary: VerdictSummary) -> dict[str, Any]:
    """JSON form of a summary (tuples become lists), plus its label."""
    data = dataclasses.asdict(summary)
    data["label"] = summary.label
    return data


def summary_from_json(data: Mapping[str, Any]) -> VerdictSummary:
    """Rebuild a :class:`VerdictSummary` stored by :func:`persist_verdict`."""
    values = {k: v for k, v in data.items() if k in {f.name for f in dataclasses.fields(VerdictSummary)}}
    values["function_rows"] = tuple(_function_row(row) for row in values.get("function_rows", ()))
    values["missing_input_classes"] = tuple(
        MissingClass(**m) for m in values.get("missing_input_classes", ())
    )
    values["invariants"] = tuple(InvariantRow(**i) for i in values.get("invariants", ()))
    values["invariants_changed"] = tuple(InvariantRow(**i) for i in values.get("invariants_changed", ()))
    values["failures"] = tuple(FailureRow(**f) for f in values.get("failures", ()))
    return VerdictSummary(**values)


def _function_row(row: Mapping[str, Any]) -> FunctionRow:
    return FunctionRow(
        **{
            **row,
            "input_classes_missing": tuple(row.get("input_classes_missing", ())),
            "invariants": tuple(InvariantRow(**i) for i in row.get("invariants", ())),
        }
    )


async def persist_verdict(session: AsyncSession, run: Run, summary: VerdictSummary) -> None:
    """Store the conclusion, reason and the full summary on the run row."""
    run.verdict = summary.conclusion
    run.reason = summary.reason
    data = summary_to_json(summary)
    data["rank_executed"] = run.rank_executed
    if run.t_first_failure_file_order_est is not None:
        data["t_first_failure_file_order_est_label"] = ESTIMATE_LABEL
    run.summary_json = data
    await session.flush()


# -- forge --------------------------------------------------------------------------------------


async def post_summary(session: AsyncSession, run: Run, summary: VerdictSummary, forge: Any) -> list[str]:
    """Post ``summary`` through ``forge`` when the run belongs to a PR; store the returned ids.

    Returns forge error strings (posting is best effort and never fails the run).
    """
    if forge is None or run.pr_id is None:
        return []
    from coverset.forge.verdict import post_verdict

    pr = await session.get(PullRequest, run.pr_id)
    repo = await session.get(Repo, run.repo_id)
    if pr is None or repo is None:
        return []
    try:
        posted = await post_verdict(
            forge,
            repo_ref(repo),
            pr.number,
            run.head_sha,
            summary,
            check_id=run.check_run_id,
            comment_id=pr.comment_id,
            external_id=run.id,
        )
    except Exception as exc:  # never let the forge fail a run
        log.warning("posting the verdict of run %s failed: %s", run.id, exc)
        return [str(exc)]
    run.check_run_id = posted.check_id
    pr.comment_id = posted.comment_id
    await session.flush()
    return list(posted.errors)


def repo_ref(repo: Repo) -> Any:
    """A forge ``RepoRef`` for ``repo`` (local repos get a numeric id derived from their hash)."""
    from coverset.forge.base import RepoRef, repo_ref_from_model

    if str(repo.external_id).isdigit():
        return repo_ref_from_model(repo)
    return RepoRef(
        external_id=int(str(repo.external_id)[:12], 16),
        full_name=repo.full_name,
        owner_id=repo.owner_id,
        private=repo.private,
        default_branch=repo.default_branch,
        installation_id=repo.installation_id,
        id=repo.id,
    )
