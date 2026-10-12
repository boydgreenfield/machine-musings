"""``coverset report --compare``: what one run gained or lost against a base run.

:func:`compare` reads two :class:`~coverset.report.facts.ReportInput` (any two sources the report
accepts) and returns a :class:`Comparison`, which the build turns into ``changes.html``, the index
tile, the "since base" gutter marks of the file pages and the one-line summaries of the function
pages. Both sides are read with the same code the pages use (``RunFacts.line_facts``,
``tests_in_range``, ``function_view``), so a number on the Changes page is the difference of two
numbers the two reports would show.

The rules (``docs/report.md``, "What the Changes page compares"):

* files and functions are matched by path and by ``(path, qualname, ordinal)``, never by line number;
* a file is **unchanged** when its statement and excluded sets are equal on both sides and, where the
  base's own text is known (a facts-store base reads its sources at its own head), the texts are
  equal; two facts-store runs with different heads whose text cannot be read count as changed.
  Lines of an unchanged file are compared one to one;
* in a **changed** file, lines are compared only inside functions present on both sides whose
  ``body_hash`` is equal and whose body statements line up after shifting by the ``def`` line
  ("body unchanged, coverage changed"); a function whose body changed is compared by counts
  ("body changed": statements ran of its body, base and now). Module-level code of a changed file
  is not compared line by line;
* a base's function index is its own when it came from the facts store, from the bundle's shipped
  ``functions`` rows, or from sources read at the base's head. A base bundle whose Python regions were
  indexed from the *local* tree borrows the current text: those regions are used only for files
  that are unchanged by the statement rule, never for "body unchanged" claims;
* a line, input class or covering test counts as **lost** only when some test (or import time) that
  ran it in the base ran in this run too; otherwise it is "not re-run" (the two runs ran different
  test sets), reported apart and never counted as lost.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from coverset.facts.schema import CALLABLE_KINDS
from coverset.lang.base import IMPORT_CONTEXT, FunctionRegion
from coverset.report.facts import NO_RESULT, NOT_RUN, ReportInput, TestInfo
from coverset.web.facts_view import FunctionView
from coverset.web.render.source import LineFacts

__all__ = [
    "SLOWER_FACTOR",
    "SLOWER_MIN_S",
    "Comparison",
    "FileDelta",
    "FnDelta",
    "TestDelta",
    "TotalRow",
    "compare",
    "side_totals",
]

SLOWER_FACTOR = 2.0
"""A test is "slower" when it took more than this many times its base duration ..."""
SLOWER_MIN_S = 0.1
"""... and more than this many seconds longer."""

FnKey = tuple[str, str, int]
"""``(path, qualname, ordinal)``: how functions are matched across the two runs."""


# -- one side ------------------------------------------------------------------------------------


@dataclass(slots=True)
class FnSide:
    """One callable on one side."""

    region: FunctionRegion
    view: FunctionView
    tests: tuple[str, ...]
    """Tests (not ``<import>``) that ran a line of the body."""
    body: frozenset[int]
    """Body statements (not excluded) in ``[body_start, end_line]``."""
    ran: frozenset[int]
    """The body statements that ran (a one-line function's ``def`` line only when a test ran it)."""

    @property
    def observed(self) -> frozenset[str]:
        """Expected input classes some test passed in (none without observations, as on the pages)."""
        if not self.view.observations:
            return frozenset()
        cov = self.view.cov
        return frozenset(c.name for c in cov.expected if c.name in cov.observed)

    @property
    def expected(self) -> frozenset[str]:
        return frozenset(c.name for c in self.view.cov.expected)


class Side:
    """One run as the comparison reads it."""

    def __init__(self, inp: ReportInput, views: Mapping[str, FunctionView] | None = None) -> None:
        self.inp = inp
        facts = inp.facts
        self.statics = dict(facts.statics)
        self.lines: dict[str, LineFacts] = {p: facts.line_facts(p) for p in sorted(self.statics)}
        tests: dict[str, TestInfo] = {t.nodeid: t for t in inp.tests}
        for nodeid in inp.coverage:
            if nodeid != IMPORT_CONTEXT and nodeid not in tests:
                tests[nodeid] = TestInfo(nodeid, nodeid.split("::", 1)[0], NO_RESULT)
        self.tests = tests
        self.ran = frozenset(n for n, t in tests.items() if (t.outcome or NOT_RUN) != NOT_RUN)
        """Tests that ran (any outcome but "not run")."""
        self.contexts = self.ran | ({IMPORT_CONTEXT} if IMPORT_CONTEXT in inp.coverage else frozenset())
        self.functions: dict[FnKey, FnSide] = {}
        self.indexed: set[str] = set()
        views = views or {}
        for region in sorted(facts.regions, key=lambda r: (r.path, r.def_line, r.qualname)):
            if region.path not in self.statics:
                continue
            self.indexed.add(region.path)
            if region.kind not in CALLABLE_KINDS:
                continue
            view = views.get(region.func_key)
            if view is None:
                view = facts.function_view(region, inp.sources.get(region.path))
            lf = self.lines[region.path]
            body = frozenset(
                n for n in lf.statements - lf.excluded if region.body_start <= n <= region.end_line
            )
            tests_here = tuple(facts.tests_in_range(region.path, region.body_start, region.end_line))
            ran = set(body & lf.executed)
            if region.body_start <= region.def_line and not facts.tests_in_range(
                region.path, region.def_line, region.def_line
            ):
                ran.discard(region.def_line)  # import time runs the def line of a one-liner
            key = (region.path, region.qualname, region.ordinal)
            self.functions.setdefault(key, FnSide(region, view, tests_here, body, frozenset(ran)))
        self._line_tests: dict[str, dict[int, set[str]]] = {}

    def statements(self, path: str) -> frozenset[int]:
        lf = self.lines[path]
        return frozenset(lf.statements - lf.excluded)

    def executed(self, path: str) -> frozenset[int]:
        return frozenset(self.statements(path) & self.lines[path].executed)

    def line_tests(self, path: str) -> dict[int, set[str]]:
        """line → the tests (and ``<import>``) that ran it, from the coverage."""
        if path not in self._line_tests:
            self.prime([path])
        return self._line_tests[path]

    def prime(self, paths: Iterable[str]) -> None:
        """Index the tests behind every line of ``paths`` in one pass over the coverage."""
        wanted = {p for p in paths if p not in self._line_tests}
        if not wanted:
            return
        found: dict[str, dict[int, set[str]]] = {p: defaultdict(set) for p in wanted}
        for nodeid, by_path in self.inp.coverage.items():
            for path in wanted.intersection(by_path):
                lines = found[path]
                for line in by_path[path]:
                    lines[line].add(nodeid)
        self._line_tests.update(found)

    def own_regions(self, path: str) -> bool:
        """Whether this side's function index of ``path`` is its own (not indexed from a local text
        that may be another commit's)."""
        if self.inp.commit is not None:
            return True
        return not (path.endswith(".py") and self.inp.sources.get(path) is not None)


def side_totals(side: Side) -> dict[str, Any]:
    """The index's totals of one side (the same rules as the index of its own report)."""
    statements = run = missed = branches = taken = partial = 0
    for path, lf in side.lines.items():
        stm = lf.statements - lf.excluded
        statements += len(stm)
        run += sum(1 for n in stm if n in lf.executed)
        missed += sum(1 for n in lf.statements if lf.state(n) == "mis")
        partial += len(lf.partial)
        static = side.statics[path]
        arcs = side.inp.facts.run_arcs.get(path, frozenset())
        branches += len(static.arcs_possible)
        taken += sum(1 for arc in static.arcs_possible if arc in arcs)
    fns = list(side.functions.values())
    observed = sum(fn.view.cov.observed_expected for fn in fns if fn.view.observations)
    expected = sum(len(fn.view.cov.expected) for fn in fns if fn.view.observations)
    return {
        "statements": statements,
        "run": run,
        "missed": missed,
        "branches": branches,
        "branches_taken": taken,
        "partial": partial,
        "functions": len(fns),
        "called": sum(1 for fn in fns if fn.tests),
        "classes_observed": observed,
        "classes_expected": expected,
        "tests": len(side.tests),
        "outcomes": Counter(t.outcome or NOT_RUN for t in side.tests.values()),
    }


# -- the result ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TotalRow:
    """One "base → now (±n)" row of the totals table."""

    label: str
    base: int
    now: int
    key: str

    @property
    def delta(self) -> int:
        return self.now - self.base

    @property
    def text(self) -> str:
        return f"{self.base} → {self.now} ({_signed(self.delta)})"


@dataclass(slots=True)
class FileDelta:
    """What changed for one file."""

    path: str
    status: str
    """``added``, ``removed``, ``same`` (compared line by line) or ``changed`` (compared on
    function bodies)."""
    base_run: int = 0
    base_statements: int = 0
    now_run: int = 0
    now_statements: int = 0
    lost: tuple[int, ...] = ()
    """Lines (this run's numbering) that ran in the base and not now, with a base test that ran now."""
    gained: tuple[int, ...] = ()
    """Lines (this run's numbering) that ran now and not in the base."""
    not_rerun: tuple[int, ...] = ()
    """Lines that ran in the base, not now, and none of their base tests ran now."""
    why: str = ""
    """Why the file counts as changed."""
    compared_fns: int = 0
    changed_fns: int = 0
    uncompared: int = 0
    """Statements of a changed file outside the function bodies compared line by line."""

    @property
    def interesting(self) -> bool:
        return (
            self.status in ("added", "removed")
            or bool(self.lost or self.gained or self.not_rerun)
            or (self.status == "changed")
        )


@dataclass(slots=True)
class FnDelta:
    """What changed for one function."""

    path: str
    qualname: str
    ordinal: int
    status: str
    """``new``, ``removed``, ``same`` (file unchanged), ``body unchanged`` or ``body changed``."""
    func_key: str | None = None
    """This run's func_key (None for a removed function)."""
    base_body: tuple[int, int] | None = None
    now_body: tuple[int, int] | None = None
    """(body statements that ran, body statements)."""
    lost_lines: tuple[int, ...] = ()
    gained_lines: tuple[int, ...] = ()
    classes_lost: tuple[str, ...] = ()
    classes_not_rerun: tuple[str, ...] = ()
    classes_gained: tuple[str, ...] = ()
    classes_dropped: tuple[str, ...] = ()
    """Observed in the base, no longer expected now (the declared types changed)."""
    missing_reason: str = ""
    """This run's label for a missing class ("never exercised" or "not observed in N sampled calls")."""
    uncovered: bool = False
    """Called by a test in the base, by none now, and some of those tests ran now."""
    uncovered_not_rerun: bool = False
    newly_called: bool = False

    @property
    def interesting(self) -> bool:
        return (
            self.status in ("new", "removed", "body changed")
            or bool(self.lost_lines or self.gained_lines)
            or bool(
                self.classes_lost or self.classes_gained or self.classes_not_rerun or self.classes_dropped
            )
            or self.uncovered
            or self.uncovered_not_rerun
            or self.newly_called
        )

    def summary(self) -> str:
        """The function page's one-line "since base" text."""
        parts: list[str] = []
        if self.status == "new":
            parts.append("new function (not in the base)")
        elif self.status in ("body changed", "body unchanged"):
            parts.append(self.status)
        if self.status == "body changed" and self.base_body and self.now_body:
            parts.append(
                f"body statements ran {self.base_body[0]} of {self.base_body[1]} → "
                f"{self.now_body[0]} of {self.now_body[1]}"
            )
        if self.lost_lines:
            parts.append(f"{_plural(len(self.lost_lines), 'line')} newly uncovered")
        if self.gained_lines:
            parts.append(f"{_plural(len(self.gained_lines), 'line')} newly covered")
        if self.classes_lost:
            parts.append("input classes lost: " + ", ".join(self.classes_lost))
        if self.classes_not_rerun:
            parts.append("not re-run: " + ", ".join(self.classes_not_rerun))
        if self.classes_gained:
            parts.append("input classes gained: " + ", ".join(self.classes_gained))
        if self.classes_dropped:
            parts.append("no longer expected: " + ", ".join(self.classes_dropped))
        if self.uncovered:
            parts.append("no test calls it any more")
        elif self.uncovered_not_rerun:
            parts.append("its base tests did not run")
        if self.newly_called:
            parts.append("newly called by a test")
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class TestDelta:
    """One test that was added, removed, changed outcome or got slower."""

    __test__ = False

    nodeid: str
    kind: str
    """``added``, ``removed``, ``outcome`` or ``slower``."""
    base_outcome: str | None = None
    now_outcome: str | None = None
    base_s: float | None = None
    now_s: float | None = None


@dataclass(slots=True)
class Comparison:
    """Everything the Changes page, the index tile and the per-page marks show."""

    base_origin: str
    now_origin: str
    totals: list[TotalRow]
    files: list[FileDelta]
    functions: list[FnDelta]
    tests: list[TestDelta]
    base_only: list[str]
    """Tests that ran in the base and not in this run (removed, or not selected)."""
    now_only: int
    """How many tests ran now and not in the base."""
    notes: list[str] = field(default_factory=list)
    marks: dict[str, tuple[frozenset[int], frozenset[int]]] = field(default_factory=dict)
    """path → (newly uncovered, newly covered) lines, this run's numbering."""
    by_func: dict[str, FnDelta] = field(default_factory=dict)
    """This run's func_key → its delta (only functions with something to say)."""

    @property
    def lines_lost(self) -> int:
        return sum(len(f.lost) for f in self.files)

    @property
    def lines_gained(self) -> int:
        return sum(len(f.gained) for f in self.files)

    @property
    def lines_not_rerun(self) -> int:
        return sum(len(f.not_rerun) for f in self.files)

    @property
    def classes_lost(self) -> int:
        return sum(len(f.classes_lost) for f in self.functions)

    @property
    def classes_gained(self) -> int:
        return sum(len(f.classes_gained) for f in self.functions)

    @property
    def tests_changed(self) -> int:
        """Tests added, removed or with another outcome (slower tests are listed, not counted)."""
        return sum(1 for t in self.tests if t.kind in ("added", "removed", "outcome"))

    def tests_of(self, kind: str) -> list[TestDelta]:
        return [t for t in self.tests if t.kind == kind]

    def files_of(self, *status: str) -> list[FileDelta]:
        return [f for f in self.files if f.status in status]

    def functions_of(self, *status: str) -> list[FnDelta]:
        return [f for f in self.functions if f.status in status]


# -- comparing -----------------------------------------------------------------------------------


def compare(
    base: ReportInput, now: ReportInput, *, now_views: Mapping[str, FunctionView] | None = None
) -> Comparison:
    """Compare ``now`` against ``base`` (``now_views``: the build's function views of ``now``, by
    func_key, so they are not computed twice)."""
    b = Side(base)
    n = Side(now, now_views)
    totals = _total_rows(side_totals(b), side_totals(n))
    notes: list[str] = []
    files: list[FileDelta] = []
    fn_lines: dict[FnKey, tuple[tuple[int, ...], tuple[int, ...]]] = {}
    body_status: dict[FnKey, str] = {}
    changed_paths: set[str] = set()
    b.prime(p for p in b.statics if p in n.statics and b.executed(p) - n.lines[p].executed)
    for path in sorted(set(b.statics) | set(n.statics)):
        if path not in b.statics:
            files.append(
                FileDelta(
                    path, "added", now_run=len(n.executed(path)), now_statements=len(n.statements(path))
                )
            )
            continue
        if path not in n.statics:
            files.append(
                FileDelta(
                    path, "removed", base_run=len(b.executed(path)), base_statements=len(b.statements(path))
                )
            )
            continue
        delta = FileDelta(
            path,
            "same",
            base_run=len(b.executed(path)),
            base_statements=len(b.statements(path)),
            now_run=len(n.executed(path)),
            now_statements=len(n.statements(path)),
        )
        why = _changed(b, n, path)
        if why is None:
            lost_all = b.executed(path) - n.executed(path)
            gained = n.executed(path) - b.executed(path)
            lost, gone = _split(lost_all, b.line_tests(path), n.contexts, shift=0)
            delta.lost, delta.gained, delta.not_rerun = lost, tuple(sorted(gained)), gone
        else:
            changed_paths.add(path)
            delta.status, delta.why = "changed", why
            _compare_changed(b, n, path, delta, fn_lines, body_status)
        files.append(delta)
    files.sort(key=lambda f: (-len(f.lost), -len(f.not_rerun), f.status not in ("added", "removed"), f.path))

    functions: list[FnDelta] = []
    unknown_paths: set[str] = set()
    for key in sorted(set(b.functions) | set(n.functions)):
        path = key[0]
        bf = b.functions.get(key)
        nf = n.functions.get(key)
        base_usable = path not in b.statics or path not in changed_paths or b.own_regions(path)
        if bf is not None and not base_usable:
            bf = None
        if nf is not None and bf is None:
            if path in b.statics and (path not in b.indexed or not base_usable):
                unknown_paths.add(path)
                continue
            functions.append(_new_fn(nf, key))
            continue
        if bf is not None and nf is None:
            if path in n.statics and path not in n.indexed:
                unknown_paths.add(path)
                continue
            functions.append(
                FnDelta(key[0], key[1], key[2], status="removed", base_body=(len(bf.ran), len(bf.body)))
            )
            continue
        assert bf is not None and nf is not None
        functions.append(_both(b, n, key, bf, nf, path in changed_paths, fn_lines, body_status))
    functions = [f for f in functions if f.interesting]
    functions.sort(
        key=lambda f: (
            -(len(f.classes_lost) + len(f.lost_lines) + int(f.uncovered)),
            _FN_ORDER.get(f.status, 9),
            f.path,
            f.qualname,
        )
    )

    tests = _tests(b, n)
    base_only = sorted(b.ran - n.ran)
    now_only = len(n.ran - b.ran)
    if changed_paths:
        notes.append(
            f"{_plural(len(changed_paths), 'file')} changed between the two runs: lines there are compared "
            "on function bodies with the same qualname, not on line numbers."
        )
    if unknown_paths:
        notes.append(
            f"The functions of {_plural(len(unknown_paths), 'file')} could not be matched: one side has no "
            "function index for them (its sources were not read, or the base's were read from the local "
            "tree, which no longer matches the base's statements)."
        )
    if base.commit is None and base.sources and any(v is not None for v in base.sources.values()):
        notes.append(
            "The base's sources were read from the local tree, not from the base's own commit: its "
            "function boundaries are trusted only for files whose statements did not change."
        )

    marks: dict[str, tuple[frozenset[int], frozenset[int]]] = {}
    for f in files:
        if f.lost or f.gained:
            marks[f.path] = (frozenset(f.lost), frozenset(f.gained))
    by_func = {f.func_key: f for f in functions if f.func_key is not None and f.summary()}
    return Comparison(
        base_origin=base.origin,
        now_origin=now.origin,
        totals=totals,
        files=[f for f in files if f.interesting],
        functions=functions,
        tests=tests,
        base_only=base_only,
        now_only=now_only,
        notes=notes,
        marks=marks,
        by_func=by_func,
    )


_FN_ORDER = {"body changed": 0, "body unchanged": 1, "same": 2, "new": 3, "removed": 4}


def _changed(b: Side, n: Side, path: str) -> str | None:
    """Why ``path`` counts as changed between the two runs, or None when it is compared line by line."""
    bl, nl = b.lines[path], n.lines[path]
    if bl.statements != nl.statements or bl.excluded != nl.excluded:
        return "its statements differ"
    bt, nt = b.inp.sources.get(path), n.inp.sources.get(path)
    if b.inp.commit is not None:
        if n.inp.commit == b.inp.commit:
            return None
        if bt is not None and nt is not None:
            return "its text differs" if bt != nt else None
        if n.inp.commit is not None:
            return "the two runs have different head commits and its text could not be read at both"
    return None


def _split(
    lines: Iterable[int], tests: Mapping[int, set[str]], ran_now: frozenset[str], *, shift: int
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Base lines (base numbering) that did not run now, as (lost, not re-run) in this run's numbering
    (``+ shift``): lost when some base test (or import time) that ran the line ran now too."""
    lost: list[int] = []
    gone: list[int] = []
    for line in sorted(lines):
        by = tests.get(line, set())
        (lost if (by & ran_now or not by) else gone).append(line + shift)
    return tuple(lost), tuple(gone)


def _compare_changed(
    b: Side,
    n: Side,
    path: str,
    delta: FileDelta,
    fn_lines: dict[FnKey, tuple[tuple[int, ...], tuple[int, ...]]],
    body_status: dict[FnKey, str],
) -> None:
    """Lines of a changed file, compared inside function bodies that did not change."""
    lost: set[int] = set()
    gained: set[int] = set()
    gone: set[int] = set()
    covered: set[int] = set()
    usable = b.own_regions(path)
    for key, nf in n.functions.items():
        if key[0] != path:
            continue
        bf = b.functions.get(key) if usable else None
        if bf is None:
            continue
        shift = nf.region.def_line - bf.region.def_line
        same_hash = bool(bf.region.body_hash) and bf.region.body_hash == nf.region.body_hash
        if same_hash and frozenset(x + shift for x in bf.body) == nf.body:
            body_status[key] = "body unchanged"
            delta.compared_fns += 1
            covered |= nf.body
            fn_lost, fn_gone = _split(
                bf.ran - {x - shift for x in nf.ran}, b.line_tests(path), n.contexts, shift=shift
            )
            fn_gained = tuple(sorted(nf.ran - {x + shift for x in bf.ran}))
            fn_lines[key] = (fn_lost, fn_gained)
            lost |= set(fn_lost)
            gone |= set(fn_gone)
            gained |= set(fn_gained)
        else:
            body_status[key] = "body changed"
            delta.changed_fns += 1
    delta.lost = tuple(sorted(lost))
    delta.gained = tuple(sorted(gained))
    delta.not_rerun = tuple(sorted(gone))
    delta.uncompared = len(n.statements(path) - covered)


def _new_fn(nf: FnSide, key: FnKey) -> FnDelta:
    return FnDelta(
        key[0],
        key[1],
        key[2],
        status="new",
        func_key=nf.region.func_key,
        now_body=(len(nf.ran), len(nf.body)),
        classes_gained=tuple(sorted(nf.observed)),
        newly_called=bool(nf.tests),
        missing_reason=nf.view.cov.missing_reason,
    )


def _both(
    b: Side,
    n: Side,
    key: FnKey,
    bf: FnSide,
    nf: FnSide,
    file_changed: bool,
    fn_lines: Mapping[FnKey, tuple[tuple[int, ...], tuple[int, ...]]],
    body_status: Mapping[FnKey, str],
) -> FnDelta:
    path = key[0]
    if file_changed:
        status = body_status.get(key, "body changed")
        lost_lines, gained_lines = fn_lines.get(key, ((), ()))
    else:
        status = "same"
        lost_all = bf.ran - nf.ran
        lost_lines, _ = _split(lost_all, b.line_tests(path), n.contexts, shift=0)
        gained_lines = tuple(sorted(nf.ran - bf.ran))
    lost: list[str] = []
    gone: list[str] = []
    dropped: list[str] = []
    expected_now = nf.expected
    for name in sorted(bf.observed - nf.observed):
        if name not in expected_now:
            dropped.append(name)
            continue
        passed_in = set(bf.view.cov.tests.get(name, ()))
        (lost if (not passed_in or passed_in & n.ran) else gone).append(name)
    gained = tuple(sorted(nf.observed - bf.observed))
    uncovered = uncovered_not_rerun = False
    if bf.tests and not nf.tests:
        if set(bf.tests) & n.ran:
            uncovered = True
        else:
            uncovered_not_rerun = True
    return FnDelta(
        key[0],
        key[1],
        key[2],
        status=status,
        func_key=nf.region.func_key,
        base_body=(len(bf.ran), len(bf.body)),
        now_body=(len(nf.ran), len(nf.body)),
        lost_lines=tuple(lost_lines),
        gained_lines=tuple(gained_lines),
        classes_lost=tuple(lost),
        classes_not_rerun=tuple(gone),
        classes_gained=gained,
        classes_dropped=tuple(dropped),
        missing_reason=nf.view.cov.missing_reason,
        uncovered=uncovered,
        uncovered_not_rerun=uncovered_not_rerun,
        newly_called=bool(nf.tests) and not bf.tests,
    )


def _tests(b: Side, n: Side) -> list[TestDelta]:
    out: list[TestDelta] = []
    for nodeid in sorted(set(b.tests) | set(n.tests)):
        bt = b.tests.get(nodeid)
        nt = n.tests.get(nodeid)
        if bt is None and nt is not None:
            out.append(TestDelta(nodeid, "added", now_outcome=nt.outcome or NOT_RUN, now_s=nt.duration_s))
            continue
        if nt is None and bt is not None:
            out.append(TestDelta(nodeid, "removed", base_outcome=bt.outcome or NOT_RUN, base_s=bt.duration_s))
            continue
        assert bt is not None and nt is not None
        before, after = bt.outcome or NOT_RUN, nt.outcome or NOT_RUN
        if before != after:
            out.append(TestDelta(nodeid, "outcome", before, after, bt.duration_s, nt.duration_s))
        if (
            bt.duration_s is not None
            and nt.duration_s is not None
            and nt.duration_s > SLOWER_FACTOR * bt.duration_s
            and nt.duration_s - bt.duration_s > SLOWER_MIN_S
        ):
            out.append(TestDelta(nodeid, "slower", before, after, bt.duration_s, nt.duration_s))
    return out


def _total_rows(base: dict[str, Any], now: dict[str, Any]) -> list[TotalRow]:
    rows = [
        TotalRow("Statements", base["statements"], now["statements"], "statements"),
        TotalRow("Lines run", base["run"], now["run"], "run"),
        TotalRow("Lines never ran", base["missed"], now["missed"], "missed"),
        TotalRow("Branches", base["branches"], now["branches"], "branches"),
        TotalRow("Branches taken", base["branches_taken"], now["branches_taken"], "branches-taken"),
        TotalRow("Partial lines", base["partial"], now["partial"], "partial"),
        TotalRow("Functions", base["functions"], now["functions"], "functions"),
        TotalRow("Functions called by a test", base["called"], now["called"], "called"),
        TotalRow(
            "Input classes observed", base["classes_observed"], now["classes_observed"], "classes-observed"
        ),
        TotalRow(
            "Input classes expected", base["classes_expected"], now["classes_expected"], "classes-expected"
        ),
        TotalRow("Tests", base["tests"], now["tests"], "tests"),
    ]
    from coverset.report.build import outcome_rank

    outcomes = sorted(set(base["outcomes"]) | set(now["outcomes"]), key=lambda o: (outcome_rank(o), o))
    for name in outcomes:
        rows.append(
            TotalRow(
                f"Tests {name}",
                base["outcomes"].get(name, 0),
                now["outcomes"].get(name, 0),
                "tests-" + name.replace(" ", "_"),
            )
        )
    return rows


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else ("±0" if value == 0 else f"−{-value}")


def _plural(count: int, word: str) -> str:
    return f"{count} {word if count == 1 else word + 's'}"
