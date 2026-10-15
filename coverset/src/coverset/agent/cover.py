"""Coverage facts per test, for the minimal-set and redundancy tools.

The question "which tests can go?" is a set-cover problem over *facts*: every line a test ran,
every branch arc, every input class it passed in. :class:`CoverIndex` lists each fact once with the
tests that reach it, built from the run's facts:

* **lines**: the facts' per-test line table (``test_lines``), import time left out;
* **arcs**: per-test arcs are not recorded (only the run's union), so a branch arc ``a -> b`` is
  attributed to the tests that ran both ``a`` and ``b``; an exit arc ``a -> -1`` to the tests that ran
  ``a`` and none of its other destinations. Both are approximations and the tools say so;
* **classes**: the input classes of each function's view (the same catalogue as the pages), reached
  by the tests listed on each shape observation and by the test that called the function directly.

Two facts reached by exactly the same tests are interchangeable for a cover, so
:func:`blocks` groups the facts of a scope by their test tuple first: the set-cover instance has one
element per distinct tuple, typically 10-100x fewer than facts, and its answer is the same. The
cover itself is :func:`coverset.analysis.cover.greedy_cover_csr` followed by
:func:`~coverset.analysis.cover.prune_csr` (the rule wave 1 uses: most new elements per second
of the test's duration), or, with a time budget, :func:`budgeted_cover` (most new facts per second
until the budget is spent).
"""

from __future__ import annotations

import heapq
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

import numpy as np

from coverset.agent.context import RunContext
from coverset.analysis.cover import csr_of, greedy_cover_csr, prune_csr

__all__ = [
    "FACT_KINDS",
    "CoverIndex",
    "CoverResult",
    "Scope",
    "blocks",
    "budgeted_cover",
    "cover_index",
    "solve",
]

FactKind = Literal["lines", "arcs", "classes"]
FACT_KINDS: Final[tuple[FactKind, ...]] = ("lines", "arcs", "classes")
_MIN_WEIGHT_S: Final = 1e-3


@dataclass(slots=True)
class CoverIndex:
    """Every fact of a run with the tests that reach it (tests are indices into ``nodeids``)."""

    nodeids: list[str]
    kind: list[FactKind] = field(default_factory=list)
    path: list[str] = field(default_factory=list)
    key: list[object] = field(default_factory=list)
    """``line`` (int), ``(from, to)`` for an arc, or the class name (``email:idn``)."""
    func: list[str | None] = field(default_factory=list)
    """The func_key of a class fact (None for lines and arcs)."""
    tests: list[tuple[int, ...]] = field(default_factory=list)
    arcs_unattributed: int = 0
    """Taken arcs no test could be credited with (left out of the facts)."""

    def add(
        self, kind: FactKind, path: str, key: object, tests: tuple[int, ...], func: str | None = None
    ) -> None:
        """Record one fact."""
        self.kind.append(kind)
        self.path.append(path)
        self.key.append(key)
        self.func.append(func)
        self.tests.append(tests)

    def __len__(self) -> int:
        return len(self.kind)

    def label(self, index: int) -> str:
        """A fact in words-ish: ``path:12``, ``path:12->14``, ``path::func email:idn``."""
        kind, path, key = self.kind[index], self.path[index], self.key[index]
        if kind == "lines":
            return f"{path}:{key}"
        if kind == "arcs":
            a, b = key  # type: ignore[misc]
            return f"{path}:{a}->{b if b > 0 else 'exit'}"
        return f"{path} {key}"


@dataclass(frozen=True, slots=True)
class Scope:
    """Which facts must stay covered: kinds, and optionally a path prefix or a set of changed lines
    and functions (for ``scope='changed'``)."""

    kinds: frozenset[FactKind] = frozenset(FACT_KINDS)
    path_prefix: str | None = None
    lines: Mapping[str, frozenset[int]] | None = None
    """``changed`` scope: path → the lines that count (changed function bodies and changed lines)."""
    functions: frozenset[str] | None = None
    """``changed`` scope: the func_keys whose input classes count."""

    def selects(self, index: CoverIndex, i: int) -> bool:
        """Whether fact ``i`` is in scope."""
        kind = index.kind[i]
        if kind not in self.kinds:
            return False
        path = index.path[i]
        if self.path_prefix is not None and not _under(path, self.path_prefix):
            return False
        if self.lines is None and self.functions is None:
            return True
        if kind == "classes":
            return self.functions is not None and index.func[i] in self.functions
        wanted = (self.lines or {}).get(path)
        if not wanted:
            return False
        key = index.key[i]
        if kind == "lines":
            return key in wanted
        a, b = key  # type: ignore[misc]
        return a in wanted or (b > 0 and b in wanted)


def _under(path: str, prefix: str) -> bool:
    """A plain prefix match (``src/ledger/billing`` holds ``src/ledger/billing/tax.py``)."""
    return path.startswith(prefix.removeprefix("./"))


def cover_index(ctx: RunContext) -> CoverIndex:
    """The run's :class:`CoverIndex` (built once per context and cached on it)."""
    found = ctx.cache.get("cover_index")
    if found is None:
        found = _build(ctx)
        ctx.cache["cover_index"] = found
    return found


def _build(ctx: RunContext) -> CoverIndex:
    facts = ctx.facts
    nodeids = sorted(set(ctx.test_ids) | set(ctx.tests))
    local = {nodeid: i for i, nodeid in enumerate(nodeids)}
    index = CoverIndex(nodeids)
    interned: dict[tuple[int, ...], tuple[int, ...]] = {}

    def intern(found: Iterable[int]) -> tuple[int, ...]:
        key = tuple(sorted(set(found)))
        return interned.setdefault(key, key)

    # lines: group the (file, line, test) table by (file, line), import time left out
    files, lines, tests = ctx.line_table
    keep = tests != 0
    files, lines, tests = files[keep], lines[keep], tests[keep]
    to_local = {tid: local[nodeid] for nodeid, tid in ctx.test_ids.items()}
    line_tests: dict[tuple[str, int], tuple[int, ...]] = {}
    if len(files):
        key = (files.astype(np.int64) << 32) | lines.astype(np.int64)
        cuts = np.flatnonzero(np.diff(key)) + 1
        starts = np.concatenate(([0], cuts)).tolist()
        ends = np.concatenate((cuts, [len(key)])).tolist()
        file_list, line_list, test_list = files.tolist(), lines.tolist(), tests.tolist()
        for lo, hi in zip(starts, ends, strict=True):
            path = ctx.path_of.get(file_list[lo])
            if path is None:
                continue
            found = intern(to_local[t] for t in test_list[lo:hi] if t in to_local)
            if not found:
                continue
            line_tests[(path, line_list[lo])] = found
            index.add("lines", path, line_list[lo], found)

    # arcs: attributed from the line facts (see the module docstring)
    for path, static in facts.statics.items():
        branch = facts.branch_lines.get(path, frozenset())
        if not branch:
            continue
        possible: dict[int, set[int]] = defaultdict(set)
        for a, b in static.arcs_possible:
            if a in branch:
                possible[a].add(b)
        for a, b in sorted(facts.run_arcs.get(path, frozenset())):
            if len(possible.get(a, ())) < 2 or b not in possible[a]:
                continue
            here = set(line_tests.get((path, a), ()))
            if b > 0:
                here &= set(line_tests.get((path, b), ()))
            else:
                for other in possible[a]:
                    if other > 0:
                        here -= set(line_tests.get((path, other), ()))
            if here:
                index.add("arcs", path, (a, b), intern(here))
            else:
                index.arcs_unattributed += 1

    # classes: the input classes each test passed in, through the function views
    observed = ctx.observations()
    regions = [ctx.by_key[k] for k in observed if k in ctx.by_key]
    for view in ctx.views(regions):
        region = view.region
        expected = {c.name for c in view.cov.expected}
        reach: dict[str, set[int]] = defaultdict(set)
        for obs in view.observations:
            if obs.param in ("<receiver>", "return"):
                continue
            classes = view.catalogue.observed_classes([obs]) & expected
            if not classes:
                continue
            who = {local[n] for n in obs.tests if n in local}
            if isinstance(obs.caller, str) and obs.caller in local:
                who.add(local[obs.caller])
            for name in classes:
                reach[name] |= who
        for name in sorted(reach):
            if reach[name]:
                index.add("classes", region.path, name, intern(reach[name]), region.func_key)
    return index


# -- the cover ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Blocks:
    """The facts of a scope grouped by their test tuple."""

    tests: list[tuple[int, ...]]
    """Block → the tests that reach it."""
    members: list[list[int]]
    """Block → its fact indices."""
    of_test: dict[int, set[int]]
    """Test → the blocks it reaches."""


def blocks(index: CoverIndex, selected: Iterable[int]) -> Blocks:
    """Group the ``selected`` facts by test tuple (one set-cover element per distinct tuple)."""
    by_tests: dict[tuple[int, ...], int] = {}
    tests: list[tuple[int, ...]] = []
    members: list[list[int]] = []
    of_test: dict[int, set[int]] = defaultdict(set)
    for i in selected:
        holders = index.tests[i]
        block = by_tests.get(holders)
        if block is None:
            block = by_tests[holders] = len(tests)
            tests.append(holders)
            members.append([])
            for t in holders:
                of_test[t].add(block)
        members[block].append(i)
    return Blocks(tests, members, dict(of_test))


@dataclass(frozen=True, slots=True)
class CoverResult:
    """A cover of one scope: the kept tests (pick order) and the facts left uncovered."""

    kept: list[int]
    uncovered_blocks: frozenset[int]
    blocks: Blocks
    exhaustive: bool
    """True when every fact of the scope is covered (no budget cut it short)."""


def solve(
    index: CoverIndex,
    selected: Sequence[int],
    weights: Mapping[int, float],
    *,
    budget_s: float | None = None,
) -> CoverResult:
    """Cover the ``selected`` facts with as few test-seconds as the greedy rule finds.

    Without a budget: :func:`~coverset.analysis.cover.greedy_cover_csr` then
    :func:`~coverset.analysis.cover.prune_csr` over the blocks (every fact stays covered). With ``budget_s``: :func:`budgeted_cover` (most facts per second
    within the budget; what it leaves out is reported as lost).
    """
    grouped = blocks(index, selected)
    if budget_s is None:
        order = sorted(grouped.of_test)
        offsets, elements = csr_of([grouped.of_test[t] for t in order])
        seconds = np.array([max(weights.get(t, 0.0), _MIN_WEIGHT_S) for t in order])
        chosen = prune_csr(greedy_cover_csr(offsets, elements, seconds), offsets, elements, seconds)
        return CoverResult([order[i] for i in chosen], frozenset(), grouped, True)
    sizes = [len(m) for m in grouped.members]
    kept = budgeted_cover(grouped.of_test, sizes, weights, budget_s)
    covered: set[int] = set()
    for t in kept:
        covered |= grouped.of_test[t]
    uncovered = frozenset(range(len(grouped.tests))) - covered
    return CoverResult(kept, uncovered, grouped, not uncovered)


def budgeted_cover(
    sets: Mapping[int, Collection[int]],
    sizes: Sequence[int],
    weights: Mapping[int, float],
    budget_s: float,
) -> list[int]:
    """Greedy budgeted maximum coverage: repeatedly the test with the most newly covered facts
    (``sizes`` per element) per second that still fits in what is left of ``budget_s``; ties by
    test index. Lazy like the unbudgeted cover: a gain only shrinks, so a stale entry is re-scored
    when it reaches the top."""
    remaining_budget = budget_s
    covered: set[int] = set()

    def weight(t: int) -> float:
        return max(weights.get(t, 0.0), _MIN_WEIGHT_S)

    heap = [(-sum(sizes[e] for e in elems) / weight(t), t) for t, elems in sets.items() if elems]
    heapq.heapify(heap)
    chosen: list[int] = []
    while heap:
        _, t = heapq.heappop(heap)
        if weight(t) > remaining_budget + 1e-9:
            continue
        gain = sum(sizes[e] for e in sets[t] if e not in covered)
        if gain == 0:
            continue
        entry = (-gain / weight(t), t)
        if heap and entry > heap[0]:
            heapq.heappush(heap, entry)
            continue
        chosen.append(t)
        covered |= set(sets[t])
        remaining_budget -= weight(t)
    return chosen


def kind_counts(index: CoverIndex, facts: Iterable[int]) -> dict[str, int]:
    """How many of ``facts`` are lines, arcs and classes."""
    counts = Counter(index.kind[i] for i in facts)
    return {kind: counts.get(kind, 0) for kind in FACT_KINDS}


def singletons(index: CoverIndex, kinds: Collection[FactKind]) -> Counter[int]:
    """Per test, how many facts (of ``kinds``, anywhere in the run) only that test reaches."""
    out: Counter[int] = Counter()
    for i, holders in enumerate(index.tests):
        if len(holders) == 1 and index.kind[i] in kinds:
            out[holders[0]] += 1
    return out
