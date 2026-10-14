"""Production reality against the tests: input-class gaps, archetypal calls, and the tests that cover them.

Everything here is pure: it takes production rows (from :mod:`coverset.facts.flows`) and test
observations (``call_shapes``) of one function and returns plain dataclasses, so the CLI, a future
page and the MCP tools share one derivation (``docs/production-flows.md``).

**Classes, not keys.** Production and tests are compared through the input-class catalogue
(:mod:`coverset_model.catalogue`), never by raw shape key: ``str|ascii|kind:email/plain|len:11-100``
and ``...|len:2-10`` are the same class (``email:plain``), and comparing keys would report length
noise as gaps. :func:`classify_shapes` asks the catalogue for the classes of every ``(parameter,
shape key)`` of a function *in one call*, with production and test shapes together, so the class
names (including the catalogue's collision renames such as ``str-empty``) are identical on both
sides. It works by giving each shape a marker "test id" and reading back which markers each class
collected (:attr:`FunctionInputCov.tests`). ``return`` and ``<receiver>`` are not inputs and are
left out.

**Gaps** (:func:`production_gaps`): the classes production passed in that no test did, each with its
share of the parameter's production calls, the estimated calls (scaled to the function's exact call
count when the probe reported one) and the catalogue's Hypothesis hint. Functions are ranked by gap
volume (``FunctionGaps.score``), so the list reads "most production traffic the tests never
imitated" first.

**Archetypes** (:func:`find_archetypes`): a call is the set of classes its arguments fell into (its
*itemset*). Distinct itemsets, weighted by estimated calls, are clustered bottom-up with average
linkage on Jaccard distance (Lance-Williams updates, pure Python, at most ``max_points`` itemsets;
rarer ones join the nearest cluster afterwards or stay out). An archetype is a cluster's *core*,
the items present in at least half of its weight, named by its *distinguishing* items (most frequent
inside the cluster relative to outside). Items (nearly) every call has (``email:str``,
``email:ascii``) are set aside before clustering (:func:`universal_items`): left in, they make any
two calls look half alike and Jaccard distance would merge exactly the archetypes worth telling
apart. :func:`frequent_itemsets` mines the closed frequent
itemsets (Apriori, up to three items) that the design doc proposes as the other view of the same
data. An archetype's id is a hash of its sorted core, so the same behaviour keeps its id across
windows; :func:`match_archetypes` pairs archetypes of two windows whose cores merely drifted.

**Covering tests** (:func:`match_tests`): for each test, the classes it passed to the function
(the union over its calls: ``call_shapes`` aggregates per test, not per call). *Exact*: restricted to
the archetype's parameters, the test's classes are the core. *Superset*: the test exercised every
core class and more. *Partial*: some of them, at least one of the archetype's own (not merely the
classes every call has). Because a test's classes are pooled over its calls,
"superset" can mean several calls that each covered part; "exact" from a single-call test is a true
per-call match.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Final, Literal

from coverset_model.catalogue import NON_INPUT_PARAMS, Catalogue, InputClass
from coverset_model.neutral import FunctionRegion, ShapeObs

__all__ = [
    "Archetype",
    "ClassGap",
    "FunctionGaps",
    "TestMatch",
    "archetype_strategy",
    "classify_shapes",
    "cluster_itemsets",
    "find_archetypes",
    "frequent_itemsets",
    "jaccard",
    "universal_items",
    "match_archetypes",
    "match_tests",
    "production_gaps",
]

_MARK: Final = "\0shape:"
_FAMILY_ORDER: Final = (
    "literal", "enum", "email", "quantity", "numeric-string", "iso-date", "url", "pairs", "order",
    "elements", "container", "bytes", "number", "bool", "none", "str", "type",
)  # fmt: skip
"""Most specific first: the family whose hint stands for a parameter in an archetype's strategy."""

_TYPE_HINTS: Final = {
    "none": "st.none()",
    "bool": "st.booleans()",
    "int": "st.integers()",
    "float": "st.floats()",
    "str": "st.text()",
    "bytes": "st.binary()",
    "list": "st.lists(st.integers())",
    "tuple": "st.tuples(st.integers())",
    "dict": "st.dictionaries(st.text(), st.integers())",
    "set": "st.sets(st.text())",
    "frozenset": "st.frozensets(st.text())",
}
"""Hints for a type class production passed and the tests did not (``existing_domains:set``)."""

type Pair = tuple[str, str]
"""``(parameter, shape key)``."""
type MatchKind = Literal["exact", "superset", "partial"]


def _input_param(param: str) -> bool:
    return param not in NON_INPUT_PARAMS


def classify_shapes(
    region: FunctionRegion, pairs: Iterable[Pair]
) -> tuple[dict[Pair, frozenset[str]], dict[str, InputClass]]:
    """The input classes of every ``(parameter, shape key)`` of one function, named consistently.

    Returns the classes per pair (``return``/``<receiver>`` pairs map to nothing) and every class the
    catalogue knows for these shapes, by name (with description and Hypothesis hint).
    """
    unique = list(dict.fromkeys(p for p in pairs if _input_param(p[0])))
    observations = [
        ShapeObs(
            func=region.triple,
            param=param,
            shape_key=key,
            caller="",
            samples=1,
            tests=(f"{_MARK}{i}",),
            truncated=False,
            example=None,
            example_test=None,
        )
        for i, (param, key) in enumerate(unique)
    ]
    cov = Catalogue().function_input_cov(region, observations, None)
    classes: dict[Pair, set[str]] = defaultdict(set)
    for name, markers in cov.tests.items():
        for marker in markers:
            if marker.startswith(_MARK):
                classes[unique[int(marker[len(_MARK) :])]].add(name)
    result = {pair: frozenset(classes.get(pair, ())) for pair in unique}
    return result, {c.name: c for c in cov.expected}


# -- gaps --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassGap:
    """One input class production passed to a function and no test did."""

    name: str
    param: str
    description: str
    strategy: str
    share: float
    """Share of the parameter's sampled production calls in this class (0..1)."""
    calls: float
    """Estimated production calls in this class (scaled to the function's exact call count when known)."""
    traces: int
    """Distinct traces (or subjects) that showed it: the largest count among its shapes (a lower bound)."""
    shapes: tuple[str, ...]
    """The class's production shape keys, most frequent first (at most three)."""


@dataclass(frozen=True, slots=True)
class FunctionGaps:
    """Production input classes of one function compared with its tests'."""

    region: FunctionRegion
    prod_calls: float
    """Calls in the selected windows: exact when the probe counted them, else the sampled estimate."""
    tested: bool
    """Whether some test reached the function: by line coverage when the caller knows it
    (``reached``), else because a test passed it an argument (a call_shapes row exists)."""
    prod_classes: tuple[str, ...]
    both: tuple[str, ...]
    """Production classes some test also passed in."""
    gaps: tuple[ClassGap, ...]

    @property
    def score(self) -> float:
        """Estimated production calls in untested classes (the ranking key)."""
        return sum(g.calls for g in self.gaps)


def production_gaps(
    region: FunctionRegion,
    prod: Sequence[tuple[str, str, float, int]],
    tests: Sequence[tuple[str, str, Sequence[str]]],
    *,
    func_calls: float | None = None,
    reached: bool | None = None,
) -> FunctionGaps:
    """Classes of ``prod`` rows ``(param, shape key, est. calls, traces)`` never seen in ``tests`` rows
    ``(param, shape key, nodeids)``; ``func_calls`` is the probe's exact call count, when known.

    ``reached`` is whether the run's coverage shows a test executing the function; it decides
    :attr:`FunctionGaps.tested` together with the shape rows, so a function whose shapes were not
    recorded (shapes off, a zero-parameter generator) is not reported as never tested.
    """
    pairs = [(p, k) for p, k, _, _ in prod] + [(p, k) for p, k, _ in tests]
    classes, info = classify_shapes(region, pairs)
    tested_classes: set[str] = set()
    tested_params: set[str] = set()
    for param, key, nodeids in tests:
        if nodeids:
            tested_classes |= classes.get((param, key), frozenset())
            tested_params.add(param.lstrip("*"))
    per_param: dict[str, float] = defaultdict(float)
    volume: dict[str, float] = defaultdict(float)
    traces: dict[str, int] = defaultdict(int)
    shapes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for param, key, calls, n_traces in prod:
        if not _input_param(param):
            continue
        per_param[param] += calls
        for name in classes.get((param, key), ()):
            volume[name] += calls
            traces[name] = max(traces[name], n_traces or 0)
            shapes[name][key] += calls
    sampled = max(per_param.values(), default=0.0)
    scale = func_calls / sampled if func_calls and sampled else 1.0
    gaps: list[ClassGap] = []
    for name in sorted(volume):
        if name in tested_classes or volume[name] <= 0:
            continue
        param, _, cls = name.partition(":")
        known = info.get(name)
        if known is None and param not in tested_params:
            # A type class (the only kind the catalogue observes without expecting it) of a parameter
            # no test passed at all: its specific classes are listed already, the type adds nothing.
            continue
        param_total = next((v for p, v in per_param.items() if p.lstrip("*") == param), 0.0)
        top = sorted(shapes[name].items(), key=lambda item: (-item[1], item[0]))[:3]
        gaps.append(
            ClassGap(
                name=name,
                param=param,
                description=known.description if known else f"a value of type {cls}",
                strategy=known.hypothesis_hint if known else _TYPE_HINTS.get(cls, f"st.from_type({cls})"),
                share=volume[name] / param_total if param_total else 0.0,
                calls=volume[name] * scale,
                traces=traces[name],
                shapes=tuple(key for key, _ in top),
            )
        )
    gaps.sort(key=lambda g: (-g.calls, g.name))
    prod_classes = tuple(sorted(volume))
    return FunctionGaps(
        region=region,
        prod_calls=func_calls if func_calls else sampled,
        tested=bool(reached) or any(nodeids for _, _, nodeids in tests),
        prod_classes=prod_classes,
        both=tuple(c for c in prod_classes if c in tested_classes),
        gaps=tuple(gaps),
    )


# -- archetypes --------------------------------------------------------------------------------------


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """``|a ∩ b| / |a ∪ b|`` (1.0 for two empty sets)."""
    union = len(a | b)
    return len(a & b) / union if union else 1.0


def frequent_itemsets(
    itemsets: Sequence[tuple[frozenset[str], float]], *, min_support: float = 0.05, max_size: int = 3
) -> list[tuple[frozenset[str], float]]:
    """Closed frequent itemsets (weighted support >= ``min_support``, up to ``max_size`` items), Apriori.

    Support is the weight share of the itemsets containing the candidate. *Closed*: no frequent
    superset has the same support, so "email:ascii" is not listed beside "email:ascii +
    email:plain" when every call with one has the other. Sorted by support, then size, then name.
    """
    total = sum(w for _, w in itemsets)
    if total <= 0:
        return []

    def support(candidate: frozenset[str]) -> float:
        return sum(w for items, w in itemsets if candidate <= items) / total

    level = {frozenset([item]) for items, _ in itemsets for item in items}
    frequent: dict[frozenset[str], float] = {}
    for size in range(1, max_size + 1):
        kept = {c: s for c in level if (s := support(c)) >= min_support}
        frequent.update(kept)
        if size == max_size:
            break
        keys = sorted(kept, key=sorted)
        level = {
            a | b
            for a, b in combinations(keys, 2)
            if len(a | b) == size + 1 and all((a | b) - {x} in kept for x in a | b)
        }
    closed = [
        (items, s)
        for items, s in frequent.items()
        if not any(items < other and math.isclose(s, frequent[other]) for other in frequent)
    ]
    closed.sort(key=lambda pair: (-pair[1], -len(pair[0]), sorted(pair[0])))
    return closed


def cluster_itemsets(
    itemsets: Sequence[tuple[frozenset[str], float]],
    *,
    threshold: float = 0.5,
    max_clusters: int = 8,
    max_points: int = 150,
) -> list[list[int]]:
    """Average-linkage agglomerative clustering of weighted itemsets on Jaccard distance.

    The ``max_points`` heaviest itemsets are clustered (merging while the closest pair is within
    ``threshold``, and beyond it while there are more than ``max_clusters`` clusters); each lighter
    one then joins the cluster whose heaviest member is nearest, if within ``threshold``, else is
    left out. Returns clusters as lists of indices into ``itemsets``, heaviest cluster first.
    """
    order = sorted(range(len(itemsets)), key=lambda i: (-itemsets[i][1], sorted(itemsets[i][0])))
    head, tail = order[:max_points], order[max_points:]
    clusters: dict[int, list[int]] = {i: [i] for i in head}
    weight = {i: max(itemsets[i][1], 1e-12) for i in head}
    dist: dict[tuple[int, int], float] = {}
    for a, b in combinations(head, 2):
        dist[(min(a, b), max(a, b))] = 1.0 - jaccard(itemsets[a][0], itemsets[b][0])
    while len(clusters) > 1:
        (a, b), d = min(dist.items(), key=lambda item: (item[1], item[0]))
        if d > threshold and len(clusters) <= max_clusters:
            break
        wa, wb = weight[a], weight[b]
        for k in clusters:
            if k in (a, b):
                continue
            da = dist.pop((min(a, k), max(a, k)))
            db = dist.pop((min(b, k), max(b, k)))
            dist[(min(a, k), max(a, k))] = (wa * da + wb * db) / (wa + wb)  # Lance-Williams, average
        del dist[(a, b)]
        clusters[a] += clusters.pop(b)
        weight[a] = wa + wb
        del weight[b]
    result = list(clusters.values())
    for i in tail:
        best, best_d = None, threshold
        for members in result:
            d = 1.0 - jaccard(itemsets[i][0], itemsets[members[0]][0])
            if d <= best_d:
                best, best_d = members, d
        if best is not None:
            best.append(i)
    result.sort(key=lambda members: (-sum(itemsets[i][1] for i in members), min(members)))
    return result


@dataclass(frozen=True, slots=True)
class Archetype:
    """A recurring kind of production call of one function."""

    id: str
    core: frozenset[str]
    """Classes in at least half of the cluster's weight, the common ones included."""
    common: frozenset[str]
    """Classes (nearly) every call of the function has, whatever its archetype."""
    distinguishing: tuple[str, ...]
    weight: float
    """Estimated production calls of the cluster's itemsets."""
    share: float
    """Share of the function's sampled production calls (0..1)."""
    traces: int
    members: int
    """Distinct itemsets in the cluster."""

    @property
    def name(self) -> str:
        """The distinguishing classes; ``baseline`` for calls with nothing beyond the common classes."""
        own = sorted(self.core - self.common)
        if self.distinguishing:
            return " + ".join(self.distinguishing)
        return " + ".join(own[:3]) if own else "baseline (only the common classes)"

    @property
    def params(self) -> frozenset[str]:
        """The parameters the core speaks about."""
        return frozenset(item.split(":", 1)[0] for item in self.core)


def _archetype_id(core: frozenset[str]) -> str:
    return hashlib.blake2b(json.dumps(sorted(core)).encode(), digest_size=4).hexdigest()


def universal_items(
    itemsets: Sequence[tuple[frozenset[str], float]], *, share: float = 0.95
) -> frozenset[str]:
    """Items present in at least ``share`` of the weight: true of (nearly) every call, so they say nothing.

    ``email:str`` and ``email:ascii`` are in every call of an email validator; left in, they would
    make every pair of calls look half alike and Jaccard clustering would merge the archetypes the
    rarer items tell apart.
    """
    total = sum(w for _, w in itemsets)
    if total <= 0:
        return frozenset()
    weight: dict[str, float] = defaultdict(float)
    for items, w in itemsets:
        for item in items:
            weight[item] += w
    return frozenset(item for item, w in weight.items() if w >= share * total)


def find_archetypes(
    itemsets: Sequence[tuple[frozenset[str], float, int]],
    *,
    threshold: float = 0.5,
    max_clusters: int = 8,
    max_points: int = 150,
    universal_share: float = 0.95,
) -> list[Archetype]:
    """Archetypes of one function from ``(itemset, est. calls, traces)`` rows.

    Items common to at least ``universal_share`` of the calls (:func:`universal_items`) are set
    aside before clustering and added back to every core (``Archetype.common``); itemsets that are
    equal once they are set aside are merged first. A cluster of calls with nothing beyond the
    common items is the function's *baseline* archetype.
    """
    rows = [(items, calls, traces) for items, calls, traces in itemsets if items]
    common = universal_items([(items, calls) for items, calls, _ in rows], share=universal_share)
    merged: dict[frozenset[str], list[float]] = {}
    for items, calls, traces in rows:
        slot = merged.setdefault(items - common, [0.0, 0])
        slot[0] += calls
        slot[1] = max(slot[1], traces)
    points = [(items, w) for items, (w, _) in merged.items()]
    total = sum(w for _, w in points)
    if not points or total <= 0:
        return []
    clusters = cluster_itemsets(points, threshold=threshold, max_clusters=max_clusters, max_points=max_points)
    all_items = {item for items, _ in points for item in items}
    archetypes: list[Archetype] = []
    for members in clusters:
        inside = sum(points[i][1] for i in members)
        outside = total - inside
        member_set = set(members)
        freq_in = {
            item: sum(points[i][1] for i in members if item in points[i][0]) / inside for item in all_items
        }
        freq_out = {
            item: (
                sum(w for j, (items, w) in enumerate(points) if j not in member_set and item in items)
                / outside
                if outside > 0
                else 0.0
            )
            for item in all_items
        }
        own = frozenset(item for item, f in freq_in.items() if f >= 0.5)
        scored = sorted(
            (
                (freq_in[item] - freq_out[item], item)
                for item in own
                if freq_in[item] - freq_out[item] >= 0.25
            ),
            key=lambda pair: (-pair[0], pair[1]),
        )
        core = own | common
        archetypes.append(
            Archetype(
                id=_archetype_id(core),
                core=core,
                common=common,
                distinguishing=tuple(item for _, item in scored[:3]),
                weight=inside,
                share=inside / total,
                traces=max(merged[points[i][0]][1] for i in members),
                members=len(members),
            )
        )
    return archetypes


def match_archetypes(
    previous: Sequence[Archetype], current: Sequence[Archetype], *, min_jaccard: float = 0.5
) -> dict[str, str]:
    """Current archetype id -> the previous window's archetype it continues (greedy by core Jaccard)."""
    pairs = sorted(
        ((jaccard(c.core, p.core), c.id, p.id) for c in current for p in previous),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for score, cid, pid in pairs:
        if score < min_jaccard:
            break
        if cid in mapping or pid in taken:
            continue
        mapping[cid] = pid
        taken.add(pid)
    return mapping


@dataclass(frozen=True, slots=True)
class TestMatch:
    """A test whose classes cover (part of) an archetype."""

    __test__ = False

    nodeid: str
    kind: MatchKind
    overlap: float
    """Share of the archetype's core the test exercised (1.0 for exact and superset)."""


_KIND_ORDER: Final = {"exact": 0, "superset": 1, "partial": 2}


def match_tests(archetype: Archetype, test_items: Mapping[str, frozenset[str]]) -> list[TestMatch]:
    """The tests covering ``archetype``: exact, then superset, then partial by overlap (see the module docstring)."""
    params = archetype.params
    core = archetype.core
    own = core - archetype.common or core
    matches: list[TestMatch] = []
    for nodeid, items in test_items.items():
        shared = core & items
        if not own & items:
            continue  # sharing only the classes every call has is no evidence for this archetype
        if shared == core:
            relevant = frozenset(i for i in items if i.split(":", 1)[0] in params)
            matches.append(TestMatch(nodeid, "exact" if relevant == core else "superset", 1.0))
        else:
            matches.append(TestMatch(nodeid, "partial", len(shared) / len(core)))
    matches.sort(key=lambda m: (_KIND_ORDER[m.kind], -m.overlap, m.nodeid))
    return matches


def archetype_strategy(archetype: Archetype, classes: Mapping[str, InputClass]) -> str:
    """A Hypothesis ``@given(...)`` reproducing the archetype: per parameter, its most specific class's hint."""
    by_param: dict[str, list[InputClass]] = defaultdict(list)
    for name in archetype.core:
        known = classes.get(name)
        if known is not None and known.hypothesis_hint:
            by_param[known.param].append(known)
    parts = []
    for param in sorted(by_param):
        best = min(
            by_param[param],
            key=lambda c: (_FAMILY_ORDER.index(c.family) if c.family in _FAMILY_ORDER else 99, c.name),
        )
        parts.append(f"{param}={best.hypothesis_hint}")
    return f"@given({', '.join(parts)})" if parts else ""
