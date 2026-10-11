"""``coverset invariants scan``: the advisory review of review-kind invariants at the default-branch head.

A run reviews only the functions its diff changed, so a default-branch baseline (which has no diff)
never reviews anything. The scan fills that gap for every invariant with a ``verify: {kind: review}``
entry:

1. **Which head.** The latest *completed, trusted* baseline run (trigger ``baseline`` or ``push``) of
   the repository's default branch whose facts are on disk (``--executor`` restricts it to runs on
   that executor). Its head commit is exported from the mirror into a temporary checkout, which
   supplies ``[tool.coverset]``, ``invariants.yaml``, the sources and the test bodies.
2. **The bundle.** Each invariant's target, resolved against the run's ``functions`` facts, becomes
   a *review target* (``change_kind = "review_target"``: the function did not change, it is reviewed
   for the invariant). Its covering tests are the run's tests whose lines intersect the body
   (``tests_covering``, best-ranked five with their bodies), its input classes and shapes come from
   the run's ``call_shapes``/``func_sampling`` facts and the head sources' annotations, and its
   invariant links are the tests the run recorded as covering each invariant.
3. **The review.** :func:`coverset.ai.review.review_changes` with
   ``get_review_client(settings, repo_enabled=repo.ai_review_enabled)`` (the offline fake unless
   ``COVERSET_AI=anthropic`` and the repo opted in). A finding stored earlier for the same
   ``(func_key, bundle_hash)`` is reused; new findings are stored in ``review_findings`` under the
   baseline run.
4. **The statuses.** Each review-kind invariant is evaluated by
   :func:`coverset.analysis.invariants.evaluate`: an invariant verified *only* by review gets the
   review's status (``satisfied`` when the reviewer says so for the current body hash, otherwise
   ``needs_review``; carry-forward and staleness apply as in a run); an invariant that also has
   ``test``/``property``/``runtime`` entries keeps the status its tests decided in the run (a
   review never overrides tests and never yields ``violated``), with the fresh review entry added
   to its evidence. Statuses are written to that run's ``invariant_results`` row (the key is
   ``(run_id, invariant_id)``), which becomes ``trigger = 'scan'``; the run's own status is kept as
   ``evidence.scan.run_status``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
from sqlalchemy import select

from coverset.commands.common import find_repo, load, prepare_db, run_async
from coverset.config import ExecutorKind, Settings

__all__ = ["REVIEW_TARGET", "ScanReport", "ScanRow", "invariants_scan_command", "scan_invariants"]

REVIEW_TARGET = "review_target"
"""``change_kind`` of a function reviewed for an invariant rather than for a change."""
MAX_COVERING_TESTS = 5


@dataclass(frozen=True, slots=True)
class ScanRow:
    """One review-kind invariant in a scan."""

    inv_id: str
    target: str
    verify: str
    review: str
    run_status: str | None
    status: str


@dataclass(slots=True)
class ScanReport:
    """What :func:`scan_invariants` did."""

    repo: str
    run_id: str | None = None
    head_sha: str | None = None
    tip_sha: str | None = None
    ai_source: str = "off"
    findings_stored: int = 0
    findings_reused: int = 0
    rows: list[ScanRow] = field(default_factory=list)
    note: str | None = None

    def lines(self) -> list[str]:
        """The table and summary ``coverset invariants scan`` prints."""
        out: list[str] = []
        if self.run_id is None:
            return [self.note or f"{self.repo}: nothing to scan"]
        head = (self.head_sha or "")[:12]
        out.append(f"{self.repo} at {head} (baseline run {self.run_id})")
        if self.tip_sha and self.tip_sha != self.head_sha:
            out.append(
                f"note: the default branch has moved on to {self.tip_sha[:12]}; run a baseline to scan it"
            )
        if not self.rows:
            out.append(self.note or "no review-kind invariants")
            return out
        table = [("invariant", "target", "verify", "review", "run", "scan")]
        table += [(r.inv_id, r.target, r.verify, r.review, r.run_status or "-", r.status) for r in self.rows]
        widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
        for index, row in enumerate(table):
            out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
            if index == 0:
                out.append("  ".join("-" * w for w in widths))
        out.append(
            f"{len(self.rows)} invariant(s) scanned; review: {self.ai_source}; "
            f"{self.findings_stored} finding(s) stored, {self.findings_reused} reused"
        )
        return out


@click.command("scan")
@click.option(
    "--repo",
    "repo_spec",
    default=None,
    help="Repository (id, full name or local path); default: the only one.",
)
@click.option(
    "--executor",
    type=click.Choice(["local", "modal"]),
    default=None,
    help="Only consider baseline runs on this executor (default: any).",
)
def invariants_scan_command(repo_spec: str | None, executor: str | None) -> None:
    """Review the review-kind invariants at the default-branch head and record their statuses."""
    settings = load()
    report = run_async(lambda: _scan(settings, repo_spec, executor))  # type: ignore[arg-type]
    for line in report.lines():
        click.echo(line)


async def _scan(settings: Settings, repo_spec: str | None, executor: ExecutorKind | None) -> ScanReport:
    from coverset.ai.client import ConfigError
    from coverset.db.session import Database
    from coverset.facts.store import FactsStore

    await prepare_db(settings)
    db = Database.from_url(settings.database_url)
    try:
        return await scan_invariants(
            db, settings, FactsStore(settings.facts_dir), repo_spec, executor=executor
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        await db.engine.dispose()


async def scan_invariants(
    db: Any,
    settings: Settings,
    store: Any,
    repo_spec: str | None,
    *,
    executor: ExecutorKind | None = None,
    client_factory: Callable[[Settings, bool], Any] | None = None,
) -> ScanReport:
    """Run the scan described in the module docstring and return its report.

    ``client_factory(settings, repo_enabled)`` builds the review client (default:
    :func:`coverset.ai.client.get_review_client`); tests pass a fake with their own fixtures.
    """
    from coverset.db.models import Run
    from coverset.orchestrator.history import BASELINE_TRIGGERS
    from coverset.orchestrator.repos import archive, default_branch_sha, extract_archive, mirror_path

    async with db.session() as session:
        repo = await find_repo(session, repo_spec)
        query = select(Run).where(
            Run.repo_id == repo.id,
            Run.trigger.in_(BASELINE_TRIGGERS),
            Run.status == "completed",
            Run.branch == repo.default_branch,
            Run.trusted.is_(True),
        )
        if executor is not None:
            query = query.where(Run.executor == executor)
        candidates = list(
            await session.scalars(query.order_by(Run.created_at.desc(), Run.id.desc()).limit(20))
        )
    report = ScanReport(repo=repo.full_name)
    run = next((r for r in candidates if store.run_dir(repo.id, r.id).is_dir()), None)
    if run is None:
        on = f" on executor {executor}" if executor else ""
        report.note = (
            f"{repo.full_name} has no completed baseline run of {repo.default_branch}{on} with facts yet"
        )
        return report
    report.run_id, report.head_sha = run.id, run.head_sha
    mirror = mirror_path(settings, repo.id)
    if not mirror.is_dir():
        raise click.ClickException(f"no mirror for {repo.full_name} at {mirror}; run a baseline first")
    try:
        report.tip_sha = await asyncio.to_thread(default_branch_sha, mirror, repo.default_branch)
    except Exception:
        report.tip_sha = None
    checkout = Path(tempfile.mkdtemp(prefix="coverset-scan-"))
    try:
        await asyncio.to_thread(
            extract_archive, await asyncio.to_thread(archive, mirror, run.head_sha), checkout
        )
        await _scan_checkout(db, settings, store, repo, run, checkout, report, client_factory=client_factory)
    finally:
        await asyncio.to_thread(shutil.rmtree, checkout, True)
    return report


async def _scan_checkout(
    db: Any,
    settings: Settings,
    store: Any,
    repo: Any,
    run: Any,
    checkout: Path,
    report: ScanReport,
    *,
    client_factory: Callable[[Settings, bool], Any] | None,
) -> None:
    from coverset.ai.bundle import build_review_bundle
    from coverset.ai.client import get_review_client
    from coverset.ai.review import ReviewFindingRow, review_changes, review_evidence
    from coverset.analysis.invariants import (
        InvariantContext,
        InvariantError,
        evaluate,
        load_invariants,
        resolve_target,
    )
    from coverset.db.models import InvariantResult, ReviewFinding
    from coverset.facts.queries import ParquetPriorRun
    from coverset.lang.base import FunctionChange
    from coverset.lang.python import ConfigError, PythonAdapter
    from coverset.orchestrator.verdict import _invariant_row, _previous_result, _stored_review_evidence

    adapter = PythonAdapter()
    try:
        config = await asyncio.to_thread(adapter.load_config, checkout)
    except ConfigError as exc:
        raise click.ClickException(f"[tool.coverset] at {run.head_sha[:12]} is invalid: {exc}") from None
    path = checkout / config.invariants
    if not path.is_file():
        report.note = f"no {config.invariants} at {run.head_sha[:12]} (no invariants)"
        return
    try:
        invariants = load_invariants(path.read_text(encoding="utf-8"))
    except (InvariantError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"{config.invariants} at {run.head_sha[:12]}: {exc}") from None
    reviewed = [inv for inv in invariants if any(v.kind == "review" for v in inv.verify)]
    if not reviewed:
        report.note = f"no review-kind invariants in {config.invariants}"
        return

    regions = await asyncio.to_thread(ParquetPriorRun(store, repo.id, run.id).functions)
    targets = {inv.id: resolve_target(inv, regions, config.source_root) for inv in reviewed}
    callables = {
        r.func_key: r for r in targets.values() if r is not None and r.kind in ("function", "method")
    }
    sources = _read_sources(checkout, sorted({r.path for r in regions}))
    async with db.session() as session:
        stored = {inv.id: await _stored_result(session, run, inv) for inv in reviewed}
        ranks, nodeids = await _run_tests(session, run.id)
    links = {
        inv_id: [nodeids[t] for t in row.covering_test_ids if t in nodeids]
        for inv_id, row in stored.items()
        if row
    }
    covering = await asyncio.to_thread(_covering_tests, store, repo.id, run.id, callables, ranks, checkout)
    input_covs, shapes = await asyncio.to_thread(
        _input_coverage, store, repo, run.id, callables, sources, adapter
    )

    factory = client_factory or (lambda s, enabled: get_review_client(s, repo_enabled=enabled))
    client = factory(settings, bool(repo.ai_review_enabled))
    rows: list[ReviewFindingRow] = []
    if client is not None and callables:
        async with db.session() as session:
            found = await session.scalars(
                select(ReviewFinding).where(ReviewFinding.func_key.in_(list(callables)))
            )
            existing = {(f.func_key, f.bundle_hash): ReviewFindingRow.from_db(f) for f in found}
        bundle = build_review_bundle(
            repo_name=repo.full_name,
            head_sha=run.head_sha,
            pr_number=None,
            changes=[FunctionChange(region=r, prev=None, kind=REVIEW_TARGET) for r in callables.values()],  # type: ignore[arg-type]
            sources=sources,
            diff_hunks={},
            patch_rows=None,
            covering_tests=covering,
            input_covs=input_covs,
            shapes=shapes,
            invariants=invariants,
            settings=settings,
            invariant_links=links,
            source_root=config.source_root,
            language="python",
        )
        rows = await asyncio.to_thread(
            review_changes,
            client,
            bundle,
            untrusted=False,
            settings=settings,
            existing=lambda key, digest: existing.get((key, digest)),
        )
    report.ai_source = (
        "off" if client is None else ", ".join(sorted({str(r.source) for r in rows})) or "no findings"
    )
    report.findings_reused = sum(1 for r in rows if r.reused)
    report.findings_stored = len(rows) - report.findings_reused
    evidence = review_evidence(rows)
    now = settings.now()

    async with db.session() as session:
        for row in rows:
            if not row.reused:
                session.add(ReviewFinding(run_id=run.id, created_at=now, **row.to_db_kwargs()))
        for inv in reviewed:
            region = targets[inv.id]
            model = await _invariant_row(session, run, inv)
            previous = await _previous_result(session, run, inv)
            finding = evidence.get(inv.id) or await _stored_review_evidence(session, inv, region)
            context = InvariantContext(
                results={}, links={}, target_region=region, previous_result=previous, review_finding=finding
            )
            review_only = dataclasses.replace(inv, verify=tuple(v for v in inv.verify if v.kind == "review"))
            review_result = evaluate(review_only, context)
            existing_row = await session.get(InvariantResult, (run.id, model.id))
            run_status = _run_status(existing_row)
            if len(review_only.verify) == len(inv.verify):
                status, entries = review_result.status, list(review_result.evidence.get("entries", []))
                extra = {k: v for k, v in review_result.evidence.items() if k != "entries"}
            elif existing_row is not None:
                status = existing_row.status
                kept = [
                    e for e in (existing_row.evidence or {}).get("entries", []) if e.get("kind") != "review"
                ]
                entries = kept + list(review_result.evidence.get("entries", []))
                extra = {}
            else:
                full = evaluate(inv, context)
                status, entries = full.status, list(full.evidence.get("entries", []))
                extra = {k: v for k, v in full.evidence.items() if k != "entries"}
            previous_evidence = dict(existing_row.evidence or {}) if existing_row is not None else {}
            new_evidence: dict[str, Any] = {
                **{k: v for k, v in previous_evidence.items() if k == "lens"},
                **extra,
                "entries": entries,
                "scan": {"at": now.isoformat(), "run_status": run_status, "ai_source": report.ai_source},
            }
            if existing_row is None:
                existing_row = InvariantResult(run_id=run.id, invariant_id=model.id, covering_test_ids=[])
                session.add(existing_row)
            existing_row.status = status
            existing_row.trigger = "scan"
            existing_row.target_func_key = region.func_key if region is not None else None
            existing_row.target_body_hash = region.body_hash if region is not None else None
            existing_row.evidence = new_evidence
            report.rows.append(
                ScanRow(
                    inv_id=inv.id,
                    target=region.qualname if region is not None else f"{inv.scope.target} (not found)",
                    verify=", ".join(v.kind for v in inv.verify),
                    review=_review_label(finding),
                    run_status=run_status,
                    status=status,
                )
            )
        await session.commit()


async def _stored_result(session: Any, run: Any, inv: Any) -> Any:
    """The run's own ``invariant_results`` row for ``inv`` (this spec version), or None."""
    from coverset.analysis.invariants import inv_key, spec_hash
    from coverset.db.models import Invariant as InvariantModel
    from coverset.db.models import InvariantResult

    model = await session.scalar(
        select(InvariantModel).where(
            InvariantModel.repo_id == run.repo_id,
            InvariantModel.inv_key == inv_key(inv),
            InvariantModel.spec_hash == spec_hash(inv),
        )
    )
    if model is None:
        return None
    return await session.get(InvariantResult, (run.id, model.id))


async def _run_tests(session: Any, run_id: str) -> tuple[dict[str, int], dict[int, str]]:
    """The run's tests: planned rank by nodeid, and nodeid by ``tests.id``."""
    from coverset.db.models import TestCase, TestResult

    rows = await session.execute(
        select(TestCase.id, TestCase.nodeid, TestResult.rank)
        .join(TestResult, TestResult.test_id == TestCase.id)
        .where(TestResult.run_id == run_id)
    )
    ranks: dict[str, int] = {}
    nodeids: dict[int, str] = {}
    for test_id, nodeid, rank in rows:
        nodeids[test_id] = nodeid
        ranks[nodeid] = rank if rank is not None else 10**9
    return ranks, nodeids


def _read_sources(checkout: Path, paths: Sequence[str]) -> dict[str, str]:
    """Head-side text of ``paths`` from the checkout (missing, linked or binary files are skipped)."""
    sources: dict[str, str] = {}
    for rel in paths:
        file = checkout / rel
        if not file.is_file() or file.is_symlink():
            continue
        try:
            sources[rel] = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return sources


def _covering_tests(
    store: Any,
    repo_id: int,
    run_id: str,
    regions: Mapping[str, Any],
    ranks: Mapping[str, int],
    checkout: Path,
) -> dict[str, list[tuple[str, str]]]:
    """Per target ``func_key``: the best-ranked covering tests of the run with their bodies."""
    from coverset.facts.queries import tests_covering

    result: dict[str, list[tuple[str, str]]] = {}
    bodies: dict[str, str] = {}
    for key, region in regions.items():
        tests = tests_covering(store, repo_id, run_id, region.path, region.body_start, region.end_line)
        tests = sorted(tests, key=lambda n: (ranks.get(n, 10**9), n))[:MAX_COVERING_TESTS]
        result[key] = [(n, bodies.setdefault(n, _test_body(checkout, n))) for n in tests]
    return result


def _test_body(checkout: Path, nodeid: str) -> str:
    """Source of the test function behind ``nodeid`` in the checkout ('' when not found)."""
    from coverset.analysis.functions import index_functions

    path, _, rest = nodeid.partition("::")
    qualname = rest.split("[", 1)[0].replace("::", ".")
    file = checkout / path
    if not rest or not file.is_file() or file.is_symlink():
        return ""
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for region in index_functions(path, source):
        if region.qualname == qualname:
            return "\n".join(source.splitlines()[region.def_line - 1 : region.end_line])
    return ""


def _input_coverage(
    store: Any,
    repo: Any,
    run_id: str,
    regions: Mapping[str, Any],
    sources: Mapping[str, str],
    adapter: Any,
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """Input-class coverage and shape observations of each target.

    Shapes are those of the newest default-branch run with facts (normally the scanned baseline);
    sampling comes from the scanned run.
    """
    from coverset.commands.facts import _observation, _sampling
    from coverset.facts.queries import shapes_for_function

    catalogue = adapter.shape_catalogue(dict(sources))
    covs: dict[str, Any] = {}
    observations: dict[str, list[Any]] = {}
    for key, region in regions.items():
        rows = shapes_for_function(store, repo.id, key, 1, branch=repo.default_branch)
        observations[key] = [_observation(region, row) for row in rows]
        sampling = _sampling(store, repo.id, run_id, region)
        covs[key] = catalogue.function_input_cov(region, observations[key], sampling)
    return covs, observations


def _run_status(row: Any) -> str | None:
    """The status the run itself recorded (kept across repeated scans), or None."""
    if row is None:
        return None
    if row.trigger == "scan":
        return ((row.evidence or {}).get("scan") or {}).get("run_status")
    return row.status


def _review_label(finding: Any) -> str:
    if finding is None:
        return "-"
    return f"{finding.source}: {finding.status}"
