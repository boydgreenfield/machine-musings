"""The test economy as every surface shows it: one view model, read from the store or a bundle.

Stage 1 (:mod:`coverset.analysis.economy`, :mod:`coverset.facts.economy`) computes the numbers and
stores them at ``finalize_run``. This module is the one place that turns them into what people see,
so the Economy pages, the PR card, the sticky-comment line, the static report's Economy page and
``coverset tests`` cannot disagree:

* :class:`EconomyView` is one run's economy with ids resolved: totals, the PR test tax, every
  test's row, the parametrize families and how each function is tested. It is built either from
  the facts tables (:func:`view_from_store`, what the control plane and ``--run`` read) or from an
  :class:`~coverset.analysis.economy.EconomyReport` (:func:`view_from_report`, what ``coverset
  report`` and ``coverset tests`` compute over a local bundle, :func:`economy_for_input`).
* The derived views are pure functions over it: sorting and filters (:func:`sort_tests`), the
  redundancy picture (:func:`redundancy_of`: equivalence groups, dominance, removable candidates),
  the covering set split into keeps and drops (:func:`minimal_split`) with its ``pytest``
  argument file (:func:`deselect_args`), the leverage histogram (:func:`leverage_bins`,
  :func:`render_histogram`, an inline SVG), and the PR findings (:func:`pr_findings`: new tests
  with the lowest leverage, new tests that repeat an existing test's flow) that become the
  verdict's :class:`~coverset.forge.comment.TestTaxLine` (:func:`tax_line`).
* :func:`write_economy_page` writes the static report's ``economy.html`` (plus ``economy.css``,
  extracted from the live pages' ``features.css`` so both share one stylesheet, and
  ``economy-deselect.txt``).

Nothing here recomputes the analysis for the control plane: the pages read precomputed facts (a
few small Parquet tables per run), so a page costs a handful of reads, never a per-row query.

The covering-set download is a ``pytest`` argument file, one ``--deselect=<nodeid>`` per line, used
as ``pytest @economy-deselect.txt`` (pytest 8.2 or later reads ``@file`` arguments, one per line,
spaces included). Node ids with a control character are left out: one line must stay one argument.
"""

from __future__ import annotations

import importlib.resources
import logging
import math
import re
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from markupsafe import Markup, escape

from coverset.analysis.economy import (
    ARC_WEIGHT,
    CLASS_WEIGHT,
    LINE_WEIGHT,
    REMOVABLE_CAVEAT,
    BaseSnapshot,
    EconomyPolicy,
    EconomyReport,
    FamilyEconomy,
    FunctionTesting,
    RunTotals,
    StaleLookup,
    TestEconomy,
    TestTax,
    analyse,
    base_snapshot,
    conftest_paths,
    test_paths_of,
)
from coverset.facts.economy import (
    EquivalenceGroup,
    Redundancy,
    TestEconomyRow,
    families,
    func_testing,
    pr_test_tax,
    run_economy,
    tests_economy,
)
from coverset.forge.comment import MAX_TAX_TESTS, TaxTest, TestTaxLine
from coverset.units import fmt_ms

if TYPE_CHECKING:
    from coverset.facts.store import FactsStore
    from coverset.report.facts import ReportInput

__all__ = [
    "FILTERS",
    "SORTS",
    "DuplicateFlow",
    "EconomyView",
    "LeverageBin",
    "MinimalSet",
    "PrFindings",
    "deselect_args",
    "economy_css",
    "economy_for_input",
    "input_reader",
    "leverage_bins",
    "median_leverage",
    "minimal_split",
    "pr_findings",
    "redundancy_of",
    "render_histogram",
    "sort_tests",
    "tax_line",
    "tax_line_of",
    "view_from_report",
    "view_from_store",
    "write_economy_page",
    "zero_unique_biggest",
    "fmt_leverage",
    "report_economy",
    "git_reader",
    "dir_reader",
    "row_of",
    "summary_text",
    "local_history",
    "base_test_paths",
]

SORTS: Final[Mapping[str, tuple[str, bool]]] = {
    "leverage": ("leverage", False),
    "cost": ("cost_per_fact_ms", True),
    "statements": ("statements", True),
    "asserts": ("asserts", True),
    "unique": ("unique_facts", False),
    "duration": ("duration_ms", True),
    "nodeid": ("nodeid", False),
}
"""Sort name -> (row attribute, descending by default). The defaults put the questionable tests
first: lowest leverage, most expensive per unique fact, biggest, slowest. Nulls always sort last
whatever the direction, so "not measured" never reads as best or worst; ties break by node id."""

FILTERS: Final[Mapping[str, tuple[str, Callable[[TestEconomyRow], bool]]]] = {
    "removable": ("removable candidates", lambda r: r.removable),
    "grouped": ("same flow as another test", lambda r: r.group_id is not None),
    "dominated": ("covered by a bigger test", lambda r: r.dominated_by is not None),
    "assertion_free": ("no assertion", lambda r: r.assertion_free is True),
    "stale": ("stale", lambda r: r.stale is True),
    "unmeasured": ("not measured", lambda r: not r.measured),
}
"""``only=`` filter name -> (label, predicate), the chips above the leverage table."""

BIN_EDGES: Final = (0.25, 0.5, 1.0, 2.0, 5.0)
"""Upper edges of the leverage histogram's bins after the zero bin (unique facts per statement):
0 (adds nothing alone), under a quarter, ¼–½, ½–1, 1–2, 2–5, 5 and up. Roughly logarithmic,
because leverage is a ratio whose interesting cases are the extremes."""
DESELECT_FILE: Final = "economy-deselect.txt"
STYLESHEET: Final = "economy.css"
REPORT_PAGE: Final = "economy.html"
MAX_REPORT_ROWS: Final = 500
"""The static page lists at most this many tests in its leverage table (lowest leverage first)."""
log = logging.getLogger(__name__)

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f  ]")
_CSS_BANNER: Final = re.compile(r"^/\* economy \*/\s*$", re.M)
_NEXT_BANNER: Final = re.compile(r"^/\* [a-z][a-z0-9 -]* \*/\s*$", re.M)


# -- the view model ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EconomyView:
    """One run's economy, ids resolved: what every surface renders."""

    run_id: str
    totals: RunTotals | None
    """None when the economy failed or was skipped (``notes`` says why)."""
    tax: TestTax | None
    notes: tuple[str, ...]
    tests: tuple[TestEconomyRow, ...]
    """Every test, by node id."""
    families: tuple[FamilyEconomy, ...] = ()
    """Most redundant cases first."""
    functions: tuple[FunctionTesting, ...] = ()
    """By path and qualname."""
    weights: Mapping[str, int] = field(
        default_factory=lambda: {"line": LINE_WEIGHT, "arc": ARC_WEIGHT, "class": CLASS_WEIGHT}
    )
    base_tests: frozenset[str] | None = None
    """Node ids of the run the tax compares with (None: no base, so no "new test" exists)."""
    tax_statements_measured: bool = True
    """False when the base's test files were not read: the tax's statement count is unknown."""
    computed_ms: float | None = None

    def by_nodeid(self) -> dict[str, TestEconomyRow]:
        """Rows by node id."""
        return {row.nodeid: row for row in self.tests}

    @property
    def summary(self) -> str:
        """``leverage median 0.14, 43 redundant candidates`` (:func:`summary_text`)."""
        return summary_text(self)

    @property
    def caveat(self) -> str:
        """Shown next to every removable candidate, group and dominance (docs/economy.md)."""
        return REMOVABLE_CAVEAT


def view_from_store(
    store: FactsStore,
    repo_id: int,
    run_id: str,
    *,
    threshold: float | None = None,
    min_statements: int | None = None,
) -> EconomyView | None:
    """The economy ``finalize_run`` stored for a run, or None when it never wrote one.

    ``threshold``/``min_statements`` re-flag the tax with the repository's effective policy (the
    stored flag used the policy of the moment it was computed). Six small reads; no per-row query.
    """
    run = run_economy(store, repo_id, run_id)
    if run is None:
        return None
    tax = (
        pr_test_tax(store, repo_id, run_id, threshold=threshold, min_statements=min_statements)
        if threshold is not None or min_statements is not None
        else run.tax
    )
    base_tests: frozenset[str] | None = None
    if tax is not None and tax.base_run_id:
        dim = store.read_table(repo_id, tax.base_run_id, "tests_dim")
        base_tests = frozenset(str(n) for n in dim.column("nodeid").to_pylist() if n is not None)
    return EconomyView(
        run_id=run_id,
        totals=run.totals,
        tax=tax,
        notes=run.notes,
        tests=tuple(tests_economy(store, repo_id, run_id)),
        families=tuple(families(store, repo_id, run_id)),
        functions=tuple(func_testing(store, repo_id, run_id)),
        weights=dict(run.weights),
        base_tests=base_tests,
        computed_ms=run.computed_ms,
    )


def row_of(test: TestEconomy) -> TestEconomyRow:
    """An analysis result as the reader's row (the one shape the templates know)."""
    w = test.weight
    return TestEconomyRow(
        nodeid=test.nodeid,
        path=test.path,
        family=test.family,
        measured=w.measured,
        reason=w.reason,
        statements=w.statements,
        asserts=w.asserts,
        helper_asserts=w.helper_asserts,
        raises=w.raises,
        fixtures=w.fixtures,
        helper_statements=w.helper_statements,
        file_lines=w.file_lines,
        cases=test.cases,
        lines=test.lines,
        arcs=test.arcs,
        classes=test.classes,
        observations=test.observations,
        unique_lines=test.unique_lines,
        unique_arcs=test.unique_arcs,
        unique_classes=test.unique_classes,
        unique_facts=test.unique_facts,
        leverage=test.leverage,
        duration_ms=test.duration_ms,
        cost_per_fact_ms=test.cost_per_fact_ms,
        signature=test.signature,
        group_id=test.group_id,
        dominated_by=test.dominated_by,
        in_minimal_set=test.in_minimal_set,
        removable=test.removable,
        assertion_free=test.assertion_free,
        stale=test.stale,
        empty=test.empty,
    )


def view_from_report(
    report: EconomyReport,
    run_id: str = "bundle",
    *,
    base_tests: Iterable[str] | None = None,
    tax_statements_measured: bool = True,
    notes: Sequence[str] = (),
) -> EconomyView:
    """An :class:`EconomyView` of an analysis computed in process (a local bundle)."""
    return EconomyView(
        run_id=run_id,
        totals=report.totals,
        tax=report.tax,
        notes=(*report.notes, *notes),
        tests=tuple(row_of(t) for t in sorted(report.tests, key=lambda t: t.nodeid)),
        families=tuple(sorted(report.families, key=lambda f: (-f.redundant_cases, -f.cases, f.family))),
        functions=tuple(sorted(report.functions, key=lambda f: (f.path, f.qualname))),
        weights=dict(report.weights),
        base_tests=frozenset(base_tests) if base_tests is not None else None,
        tax_statements_measured=tax_statements_measured,
    )


# -- sorting, filters, redundancy ----------------------------------------------------------------


def sort_tests(
    rows: Iterable[TestEconomyRow], sort: str = "leverage", *, reverse: bool = False, only: str | None = None
) -> list[TestEconomyRow]:
    """Rows sorted by ``sort`` (:data:`SORTS`; ``reverse`` flips its default direction), optionally
    only one kind (:data:`FILTERS`). Unknown names raise ``ValueError`` (pages check first)."""
    if sort not in SORTS:
        raise ValueError(f"unknown sort {sort!r}: one of {', '.join(SORTS)}")
    picked = list(rows)
    if only is not None:
        if only not in FILTERS:
            raise ValueError(f"unknown filter {only!r}: one of {', '.join(FILTERS)}")
        keep = FILTERS[only][1]
        picked = [r for r in picked if keep(r)]
    column, descending = SORTS[sort]
    descending = descending != reverse
    present = [r for r in picked if getattr(r, column) is not None]
    missing = sorted((r for r in picked if getattr(r, column) is None), key=lambda r: r.nodeid)
    present.sort(key=lambda r: r.nodeid)
    present.sort(key=lambda r: getattr(r, column), reverse=descending)
    return present + missing


def redundancy_of(view: EconomyView) -> Redundancy:
    """Equivalence groups (largest first, each naming the member the covering set keeps), dominated
    pairs and removable candidates (slowest first)."""
    by_group: dict[str, list[TestEconomyRow]] = defaultdict(list)
    for row in view.tests:
        if row.group_id is not None:
            by_group[row.group_id].append(row)
    groups = []
    for group_id, members in by_group.items():
        members.sort(key=lambda r: r.nodeid)
        kept = [m for m in members if m.in_minimal_set]
        keep = kept[0] if kept else min(members, key=lambda m: (m.duration_ms or 0.0, m.nodeid))
        groups.append(
            EquivalenceGroup(
                group_id,
                tuple(m.nodeid for m in members),
                round(sum(m.duration_ms or 0.0 for m in members), 3),
                keep.nodeid,
            )
        )
    groups.sort(key=lambda g: (-len(g.nodeids), g.nodeids[0]))
    removable = sorted(
        (r for r in view.tests if r.removable), key=lambda r: (-(r.duration_ms or 0.0), r.nodeid)
    )
    return Redundancy(
        groups=tuple(groups),
        dominated=tuple(sorted((r.nodeid, r.dominated_by) for r in view.tests if r.dominated_by is not None)),
        removable=tuple(removable),
        minimal_set_size=sum(1 for r in view.tests if r.in_minimal_set),
        tests=len(view.tests),
        removable_duration_ms=round(sum(r.duration_ms or 0.0 for r in removable), 3),
    )


@dataclass(frozen=True, slots=True)
class MinimalSet:
    """The duration-weighted covering set, split the way a reader acts on it."""

    keep: tuple[TestEconomyRow, ...]
    """In the covering set (by node id)."""
    drop: tuple[TestEconomyRow, ...]
    """Removable candidates: outside it, with at least one fact (slowest first)."""
    uncompared: tuple[TestEconomyRow, ...]
    """No production fact at all (skipped, or only test code ran): nothing to compare, never dropped."""
    keep_ms: float
    drop_ms: float


def minimal_split(view: EconomyView) -> MinimalSet:
    """Keeps, drops (removable candidates) and tests with nothing to compare."""
    keep = tuple(r for r in view.tests if r.in_minimal_set)
    drop = tuple(
        sorted((r for r in view.tests if r.removable), key=lambda r: (-(r.duration_ms or 0.0), r.nodeid))
    )
    uncompared = tuple(r for r in view.tests if not r.in_minimal_set and not r.removable)
    return MinimalSet(
        keep=keep,
        drop=drop,
        uncompared=uncompared,
        keep_ms=round(sum(r.duration_ms or 0.0 for r in keep), 3),
        drop_ms=round(sum(r.duration_ms or 0.0 for r in drop), 3),
    )


def deselect_args(view: EconomyView) -> str:
    """The removable candidates as a ``pytest`` argument file: ``--deselect=<nodeid>`` per line, by
    node id (``pytest @economy-deselect.txt``). No comment line: pytest would read it as a path."""
    ids = sorted(r.nodeid for r in view.tests if r.removable and not _CONTROL.search(r.nodeid))
    return "".join(f"--deselect={nodeid}\n" for nodeid in ids)


# -- the leverage histogram ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeverageBin:
    """One bar of the leverage histogram."""

    label: str
    """Axis text (``0``, ``<0.25``, ``0.25–0.5`` … ``5+``, ``n/m``)."""
    title: str
    """What the bar counts, in words (its tooltip and the screen-reader text)."""
    count: int
    kind: str
    """``zero`` (adds nothing alone), ``low`` (under 0.5), ``mid``, ``high`` or ``unmeasured``."""


def leverage_bins(rows: Iterable[TestEconomyRow]) -> list[LeverageBin]:
    """Tests per leverage bin (see :data:`BIN_EDGES`); the "not measured" bar only when some test is."""
    counts = [0] * (len(BIN_EDGES) + 2)
    unmeasured = 0
    for row in rows:
        value = row.leverage
        if value is None:
            unmeasured += 1
        elif value <= 0:
            counts[0] += 1
        else:
            index = next((i for i, edge in enumerate(BIN_EDGES) if value < edge), len(BIN_EDGES))
            counts[index + 1] += 1
    labels = ["0", f"<{BIN_EDGES[0]:g}"]
    labels += [f"{lo:g}–{hi:g}" for lo, hi in zip(BIN_EDGES, BIN_EDGES[1:], strict=False)]
    labels.append(f"{BIN_EDGES[-1]:g}+")
    bins = []
    for i, (label, count) in enumerate(zip(labels, counts, strict=True)):
        if i == 0:
            title, kind = "no unique fact: everything they cover, another test covers too", "zero"
        else:
            title = f"leverage {label} unique facts per statement"
            kind = "low" if i <= 2 else ("mid" if i <= 4 else "high")
        bins.append(LeverageBin(label, title, count, kind))
    if unmeasured:
        bins.append(
            LeverageBin("n/m", "not measured: the test's source could not be read", unmeasured, "unmeasured")
        )
    return bins


def median_leverage(rows: Iterable[TestEconomyRow]) -> float | None:
    """The median leverage of the measured tests (None when none is)."""
    values = [r.leverage for r in rows if r.leverage is not None]
    return float(statistics.median(values)) if values else None


def render_histogram(bins: Sequence[LeverageBin], *, width: float = 620.0, height: float = 190.0) -> Markup:
    """The leverage histogram as an inline SVG: one bar per bin with its count above it and its
    range below, the zero bar drawn dashed (a cue that does not depend on colour). Every number is
    formatted here; the only text is fixed labels, so nothing untrusted reaches the markup."""
    total = sum(b.count for b in bins)
    top = max((b.count for b in bins), default=0) or 1
    pad_l, pad_r, pad_t, pad_b = 8.0, 8.0, 22.0, 40.0
    slot = (width - pad_l - pad_r) / max(1, len(bins))
    bar_w = max(6.0, slot * 0.66)
    base_y = height - pad_b
    span = base_y - pad_t
    summary = ", ".join(f"{b.count} at {b.label}" for b in bins)
    parts = [
        f'<svg class="eco-hist" viewBox="0 0 {width:g} {height:g}" role="img" aria-labelledby="eco-hist-t">',
        f'<title id="eco-hist-t">Leverage of {total} tests (unique facts per test statement): {escape(summary)}</title>',
        f'<line class="eco-axis" x1="{pad_l:g}" y1="{base_y:g}" x2="{width - pad_r:g}" y2="{base_y:g}"/>',
    ]
    for i, b in enumerate(bins):
        x = pad_l + i * slot + (slot - bar_w) / 2
        h = 0.0 if b.count == 0 else max(2.0, span * b.count / top)
        y = base_y - h
        cx = x + bar_w / 2
        parts.append(
            f'<g class="eco-bin eco-{b.kind}" data-bin="{escape(b.label)}">'
            f'<rect class="eco-bar" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="2">'
            f"<title>{b.count} {'test' if b.count == 1 else 'tests'}: {escape(b.title)}</title></rect>"
            f'<text class="eco-count" x="{cx:.1f}" y="{y - 6:.1f}" text-anchor="middle">{b.count}</text>'
            f'<text class="eco-label" x="{cx:.1f}" y="{base_y + 16:.1f}" text-anchor="middle">{escape(b.label)}</text>'
            "</g>"
        )
    parts.append(
        f'<text class="eco-axis-label" x="{width / 2:.1f}" y="{height - 6:g}" text-anchor="middle">'
        "unique facts per test statement</text></svg>"
    )
    return Markup("".join(parts))


# -- the PR findings and the tax line ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DuplicateFlow:
    """A new test whose facts an existing test already has: the same set (``same flow``) or a
    strict subset of that test's (``subset``)."""

    test: TestEconomyRow
    of: str
    how: str


@dataclass(frozen=True, slots=True)
class PrFindings:
    """What a PR's new tests add: the new tests, the lowest-leverage ones, the duplicating ones."""

    new: tuple[TestEconomyRow, ...]
    lowest: tuple[TestEconomyRow, ...]
    duplicates: tuple[DuplicateFlow, ...]


def pr_findings(view: EconomyView, *, limit: int = MAX_TAX_TESTS) -> PrFindings:
    """New tests (not in the base run) with the lowest leverage, and new tests that repeat an
    existing test's flow: an equivalence group shared with an existing test, or dominated by one.
    Empty without a base run."""
    base = view.base_tests
    if base is None:
        return PrFindings((), (), ())
    new = tuple(r for r in view.tests if r.nodeid not in base)
    existing: dict[str, str] = {}
    for row in view.tests:
        if row.nodeid in base and row.group_id is not None:
            existing.setdefault(row.group_id, row.nodeid)
    duplicates = []
    for row in new:
        if row.group_id is not None and row.group_id in existing:
            duplicates.append(DuplicateFlow(row, existing[row.group_id], "same flow"))
        elif row.dominated_by is not None and row.dominated_by in base:
            duplicates.append(DuplicateFlow(row, row.dominated_by, "subset"))
    measured = [r for r in new if r.leverage is not None]
    measured.sort(key=lambda r: (r.leverage, -(r.statements or 0), r.nodeid))
    return PrFindings(new, tuple(measured[:limit]), tuple(duplicates))


def _tax_test(row: TestEconomyRow, of: str | None = None, how: str | None = None) -> TaxTest:
    leverage = round(row.leverage, 4) if row.leverage is not None else None
    return TaxTest(row.nodeid, row.statements, row.unique_facts, leverage, of, how)


def tax_line_of(view: EconomyView) -> TestTaxLine | None:
    """The verdict's :class:`~coverset.forge.comment.TestTaxLine` of a view (None without a tax)."""
    tax = view.tax
    if tax is None:
        return None
    found = pr_findings(view)
    return TestTaxLine(
        added_statements=tax.added_statements,
        removed_statements=tax.removed_statements,
        newly_covered_lines=tax.newly_covered_lines,
        new_classes=tax.new_classes,
        tax=round(tax.tax, 3) if math.isfinite(tax.tax) else 0.0,
        threshold=tax.threshold,
        flagged=tax.flagged and view.tax_statements_measured,
        base_exact=tax.base_exact,
        statements_measured=view.tax_statements_measured,
        new_tests=tax.new_tests,
        lowest_leverage=tuple(_tax_test(r) for r in found.lowest[:MAX_TAX_TESTS]),
        duplicates=tuple(_tax_test(d.test, d.of, d.how) for d in found.duplicates[:MAX_TAX_TESTS]),
        duplicate_count=len(found.duplicates),
    )


def tax_line(
    store: FactsStore, repo_id: int, run_id: str, *, threshold: float | None = None
) -> TestTaxLine | None:
    """The PR test tax of a stored run, re-flagged against ``threshold`` (the verdict's input)."""
    view = view_from_store(store, repo_id, run_id, threshold=threshold)
    return tax_line_of(view) if view is not None else None


# -- local bundles (coverset report, coverset tests) -----------------------------------------------


def input_reader(inp: ReportInput) -> Callable[[str], str | None]:
    """Read any file of a report input's source root: the measured sources it already read, then
    test files, conftests and helper modules under the same rules (``read_sources``: plain relative
    paths inside the root, only files git tracks there when it is a work tree). The root is the one
    the report chose (``meta["root"]``), never a path the bundle names."""
    from coverset.report.facts import read_sources
    from coverset.report.load import tracked_files

    known = dict(inp.sources)
    root = inp.meta.get("root") if isinstance(inp.meta, dict) else None
    if not root or inp.commit is not None:
        return lambda path: known.get(path)
    base = Path(str(root))
    tracked = tracked_files(base)
    cache: dict[str, str | None] = {}

    def read(path: str) -> str | None:
        if path in known:
            return known[path]
        if path not in cache:
            cache[path] = read_sources(base, [path], tracked=tracked).get(path)
        return cache[path]

    return read


def _git(root: Path, *args: str) -> str | None:
    from coverset.orchestrator.repos import git_env

    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            env=git_env(),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.decode("utf-8", "surrogateescape").strip() if proc.returncode == 0 else None


def local_history(root: Path) -> tuple[Any, str] | None:
    """``(GitHistory, HEAD sha)`` of the work tree holding ``root`` (a linked worktree included), or
    None when it is not one. Read-only: ``git log`` and ``git cat-file`` with the scrubbed env."""
    from coverset.facts.economy import GitHistory

    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if not git_dir or not head or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    return GitHistory(Path(git_dir)), head


def git_reader(root: Path, ref: str) -> Callable[[Sequence[str]], Mapping[str, str]]:
    """Read files as of ``ref`` in the git repository holding ``root`` (``--base-ref``), in one
    ``git cat-file --batch``. Raises ``LookupError`` when ``ref`` is not a commit there. The ref is
    resolved with ``rev-parse --verify --end-of-options``, so it can never be read as an option."""
    found = local_history(root)
    sha = _git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    if found is None or not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise LookupError(f"{ref!r} is not a commit of the git repository at {root}")
    history, _head = found

    def read(paths: Sequence[str]) -> Mapping[str, str]:
        return history.read(sha, paths)

    return read


def dir_reader(root: Path) -> Callable[[Sequence[str]], Mapping[str, str]]:
    """Read files under another checkout (``--base-root``) by the report's rules: plain relative
    paths inside it, only files git tracks there when it is a work tree."""
    from coverset.report.facts import read_sources
    from coverset.report.load import tracked_files

    base = Path(root).resolve()
    tracked = tracked_files(base)

    def read(paths: Sequence[str]) -> Mapping[str, str]:
        return {p: t for p, t in read_sources(base, paths, tracked=tracked).items() if t is not None}

    return read


def _stale_lookup(inp: ReportInput, notes: list[str]) -> StaleLookup | None:
    from coverset.facts.economy import git_stale_lookup

    root = inp.meta.get("root") if isinstance(inp.meta, dict) else None
    found = local_history(Path(str(root))) if root and inp.commit is None else None
    if found is None:
        return None  # the analysis notes "stale tests not measured"
    history, head = found
    return git_stale_lookup(history, head, notes)


def base_test_paths(bundle: Any) -> list[str]:
    """The test files of a base bundle and every conftest pytest would load for them."""
    files = set(test_paths_of(bundle).values())
    for path in list(files):
        files.update(conftest_paths(path))
    return sorted(files)


def economy_for_input(
    inp: ReportInput,
    base: ReportInput | None = None,
    *,
    base_sources: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
    history: bool = True,
    policy: EconomyPolicy | None = None,
) -> EconomyView | None:
    """The economy of a report input: the stored tables of a facts-store run, else the analysis of
    a local bundle over its sources (``analyse`` with the report's reader).

    With ``base`` the PR test tax and the new-test findings compare with it. The tax's statement
    diff needs the base's *own* test files: ``base_sources(paths)`` returns them (``coverset tests
    tax --base-ref``); without it the statement count is "not measured" (reading this checkout's
    files for the base would compare the head with itself), while newly covered lines and classes,
    new tests and duplicates are still computed; the base's production files then come from
    ``base_sources`` too (else from the base input's own reads). ``history`` enables stale tests (a
    local git walk); ``policy`` sets the tax threshold (``--threshold``).
    """
    from coverset.report.facts import BundleFacts

    facts = inp.facts
    if not isinstance(facts, BundleFacts):
        store = getattr(facts, "store", None)
        if store is None:
            return None
        view = view_from_store(store, facts.repo_id, facts.run_id)
        if view is None or base is None or view.base_tests is not None:
            return view
        return _with_base_tests(view, base)
    notes: list[str] = []
    snapshot: BaseSnapshot | None = None
    base_ids: frozenset[str] | None = None
    measured = True
    if base is not None and isinstance(base.facts, BundleFacts):
        test_sources = dict(base_sources(base_test_paths(base.facts.bundle))) if base_sources else {}
        prod_sources = {p: t for p, t in base.sources.items() if t is not None}
        if base_sources is not None:
            prod_sources = dict(base_sources(sorted(base.facts.bundle.static)))
        measured = base_sources is not None
        if not measured:
            notes.append(
                "PR test tax: test statements not measured (the base's own test files were not read)"
            )
        snapshot = base_snapshot(
            base.facts.bundle,
            run_id="base",
            exact=True,
            functions=base.facts.regions,
            test_sources=test_sources,
            sources=prod_sources,
        )
        base_ids = snapshot.tests
    elif base is not None:
        base_ids = frozenset(t.nodeid for t in base.tests)
        notes.append("PR test tax: not computed against a facts-store base (use the live pages)")
    stale = _stale_lookup(inp, notes) if history else None
    report = analyse(
        facts.bundle,
        input_reader(inp),
        functions=facts.regions,
        base=snapshot,
        stale_lookup=stale,
        policy=policy,
    )
    return view_from_report(
        report, "bundle", base_tests=base_ids, tax_statements_measured=measured, notes=notes
    )


def report_economy(inp: ReportInput, base: ReportInput | None = None) -> EconomyView | None:
    """:func:`economy_for_input` for ``coverset report``: never raises (a failure is logged and the
    report is written without its Economy page), and skips a bundle above
    :data:`~coverset.facts.economy.MAX_TEST_LINE_ROWS` per-test line rows, as ``finalize_run`` does."""
    from coverset.facts.economy import MAX_TEST_LINE_ROWS
    from coverset.report.facts import BundleFacts

    try:
        if isinstance(inp.facts, BundleFacts):
            rows = sum(
                len(lines) for by_path in inp.facts.bundle.coverage.values() for lines in by_path.values()
            )
            if rows > MAX_TEST_LINE_ROWS:
                log.warning("report: test economy skipped: %d per-test line rows", rows)
                return None
        view = economy_for_input(inp, base)
    except Exception:  # the economy is an extra page: never let it fail the report
        log.warning("report: the test economy could not be computed", exc_info=True)
        return None
    return view if view is not None and view.totals is not None else None


def _with_base_tests(view: EconomyView, base: ReportInput) -> EconomyView:
    from dataclasses import replace

    return replace(view, base_tests=frozenset(t.nodeid for t in base.tests))


# -- the static report page -------------------------------------------------------------------------


def economy_css() -> str:
    """The ``/* economy */`` section of the live pages' ``features.css``: one stylesheet for both."""
    text = (importlib.resources.files("coverset.web") / "static" / "features.css").read_text(encoding="utf-8")
    start = _CSS_BANNER.search(text)
    if start is None:
        return ""
    rest = text[start.end() :]
    stop = _NEXT_BANNER.search(rest)
    return "/* economy (from features.css) */\n" + (rest[: stop.start()] if stop else rest).strip() + "\n"


def _func_pages(builder: Any) -> dict[str, str]:
    return {fn.region.func_key: fn.page for fn in builder.functions}


def write_economy_page(builder: Any, view: EconomyView) -> int:
    """Write ``economy.html``, its stylesheet and the deselect argument file; returns pages written.

    ``builder`` is the report's page builder (``coverset.report.build._Builder``): its ``render``
    (layout, escaping, neutralisation), ``writer`` and ``test_link`` keep this page like the others.
    """
    file_pages = {path: entry.page for path, entry in builder.files.items()}
    fn_pages = _func_pages(builder)
    rows = sort_tests(view.tests, "leverage")
    builder.writer.text(STYLESHEET, economy_css())
    builder.writer.text(DESELECT_FILE, deselect_args(view))
    builder.render(
        REPORT_PAGE,
        "economy.html",
        kind="economy",
        view=view,
        rows=rows[:MAX_REPORT_ROWS],
        rows_total=len(rows),
        histogram=render_histogram(leverage_bins(view.tests)),
        median=median_leverage(view.tests),
        redundancy=redundancy_of(view),
        minimal=minimal_split(view),
        findings=pr_findings(view),
        tax_line=tax_line_of(view),
        indirect=[f for f in view.functions if f.indirect_only],
        untested=[f for f in view.functions if f.untested],
        handlers=[f for f in view.functions if f.handlers_unexercised],
        zero_big=zero_unique_biggest(view.tests),
        test_link=builder.test_link,
        file_page=file_pages.get,
        fn_page=fn_pages.get,
        deselect=DESELECT_FILE,
        stylesheet=STYLESHEET,
        ms=fmt_ms,
        lev=fmt_leverage,
        data=None,
    )
    return 1


def fmt_leverage(value: float | None) -> str:
    """``0``, ``0.38``, ``2.5``, ``14`` (two significant digits; ``–`` when not measured)."""
    if value is None:
        return "–"
    if value == 0:
        return "0"
    return f"{value:.2g}" if value < 10 else f"{value:.0f}"


def zero_unique_biggest(rows: Iterable[TestEconomyRow], limit: int = 8) -> list[TestEconomyRow]:
    """The biggest measured tests with no unique fact: the first place to look."""
    found = [r for r in rows if r.statements is not None and r.unique_facts == 0 and not r.empty]
    found.sort(key=lambda r: (-((r.statements or 0) + (r.helper_statements or 0)), r.nodeid))
    return found[:limit]


def summary_text(view: EconomyView) -> str:
    """``leverage median 0.14, 43 redundant candidates`` (the report's index tile, the CLI)."""
    median = median_leverage(view.tests)
    candidates = sum(1 for r in view.tests if r.removable)
    lead = f"leverage median {median:.2g}" if median is not None else "leverage not measured"
    return f"{lead}, {candidates} redundant candidate{'' if candidates == 1 else 's'}"
