"""Archetypes and production gaps: clustering on synthetic calls with known clusters, test matching, gaps."""

from __future__ import annotations

import random

import pytest

from coverset.analysis.archetypes import (
    Archetype,
    archetype_strategy,
    classify_shapes,
    cluster_itemsets,
    find_archetypes,
    frequent_itemsets,
    jaccard,
    match_archetypes,
    match_tests,
    production_gaps,
    universal_items,
)
from coverset_model.catalogue import InputClass
from coverset_model.neutral import FunctionRegion

PLAIN = "str|ascii|kind:email/plain|len:11-100"
SHORT_PLAIN = "str|ascii|kind:email/plain|len:2-10"
SUBDOMAIN = "str|ascii|kind:email/multi-label-domain|len:11-100"
TAGGED = "str|ascii|kind:email/plus-tag|len:11-100"
EMPTY = "str|empty|len:0"


def _region(*params: str, qualname: str = "signup") -> FunctionRegion:
    return FunctionRegion(
        func_key="0123456789abcdef",
        path="src/app/accounts.py",
        qualname=qualname,
        ordinal=0,
        def_line=10,
        body_start=11,
        end_line=20,
        body_hash="h",
        sig_hash="s",
        params=params,
        kind="function",
    )


def fs(*items: str) -> frozenset[str]:
    return frozenset(items)


# -- clustering on synthetic data -----------------------------------------------------------------

CLUSTERS = {
    "eu-plain": fs("region:eu", "email:plain", "plan:free"),
    "us-subdomain": fs("region:us", "email:multi-label-domain", "plan:team"),
    "admin-batch": fs("role:admin", "ids:multiple", "ids:duplicates"),
}


def _noisy_calls(seed: int = 1) -> list[tuple[frozenset[str], float, int]]:
    """Three known clusters (60/30/10 % of calls); each call drops one item or adds a stray one now and then."""
    rng = random.Random(seed)
    calls = []
    names = list(CLUSTERS)
    for _ in range(600):
        name = rng.choices(names, weights=[60, 30, 10])[0]
        items = set(CLUSTERS[name]) | {"common:str"}
        roll = rng.random()
        if roll < 0.15:
            items.discard(rng.choice(sorted(CLUSTERS[name])))
        elif roll < 0.25:
            items.add(rng.choice(["flag:true", "flag:false", "note:empty"]))
        calls.append((frozenset(items), 1.0, 1))
    return calls


def test_known_clusters_are_recovered() -> None:
    archetypes = find_archetypes(_noisy_calls())
    assert len(archetypes) == 3
    by_core = {name: next(a for a in archetypes if core <= a.core) for name, core in CLUSTERS.items()}
    assert len({a.id for a in by_core.values()}) == 3
    shares = {name: a.share for name, a in by_core.items()}
    assert shares["eu-plain"] == pytest.approx(0.6, abs=0.06)
    assert shares["us-subdomain"] == pytest.approx(0.3, abs=0.06)
    assert shares["admin-batch"] == pytest.approx(0.1, abs=0.04)
    for name, archetype in by_core.items():
        assert archetype.core == CLUSTERS[name] | {"common:str"}
        assert archetype.common == {"common:str"}
        assert set(archetype.distinguishing) <= CLUSTERS[name]
        assert len(archetype.distinguishing) == 3
    assert [a.share for a in archetypes] == sorted((a.share for a in archetypes), reverse=True)


def test_archetype_ids_are_stable_across_windows() -> None:
    first = find_archetypes(_noisy_calls(seed=1))
    second = find_archetypes(_noisy_calls(seed=2))
    assert {a.id for a in first} == {a.id for a in second}
    assert match_archetypes(first, second) == {a.id: a.id for a in second}


def test_drifted_cores_are_matched_by_jaccard() -> None:
    def arch(id_: str, *core: str) -> Archetype:
        return Archetype(id_, fs(*core), fs(), (), 1.0, 0.5, 1, 1)

    previous = [arch("p1", "a", "b", "c"), arch("p2", "x", "y")]
    current = [arch("c1", "a", "b", "d"), arch("c2", "x", "y", "z"), arch("c3", "q")]
    assert match_archetypes(previous, current) == {"c2": "p2", "c1": "p1"}
    assert match_archetypes(previous, current, min_jaccard=0.7) == {}


def test_universal_items_are_set_aside() -> None:
    calls = [(fs("u", "a"), 50.0), (fs("u", "b"), 49.0), (fs("a"), 1.0)]
    assert universal_items(calls) == {"u"}
    assert universal_items(calls, share=0.5) == {"u", "a"}
    assert universal_items([]) == frozenset()


def test_a_single_behaviour_is_one_baseline_archetype() -> None:
    archetypes = find_archetypes([(fs("x:str", "x:ascii"), 10.0, 3), (fs("x:str", "x:ascii"), 5.0, 4)])
    assert len(archetypes) == 1
    only = archetypes[0]
    assert only.share == 1.0 and only.traces == 4 and only.weight == 15.0
    assert only.name == "baseline (only the common classes)"
    assert find_archetypes([]) == []


def test_cluster_threshold_and_cap() -> None:
    points = [(fs("a", "b"), 1.0), (fs("a", "c"), 1.0), (fs("x"), 1.0)]
    assert sorted(map(sorted, cluster_itemsets(points, threshold=0.5))) == [[0], [1], [2]]
    assert sorted(map(sorted, cluster_itemsets(points, threshold=0.7))) == [[0, 1], [2]]
    assert len(cluster_itemsets(points, threshold=0.0, max_clusters=1)) == 1
    many = [(fs(f"i{n}"), 1.0) for n in range(10)] + [(fs("i0", "j"), 0.5)]
    clusters = cluster_itemsets(many, threshold=0.6, max_points=10)
    assert any(10 in members and 0 in members for members in clusters)  # the light point joined i0


def test_jaccard() -> None:
    assert jaccard(fs("a", "b"), fs("b", "c")) == pytest.approx(1 / 3)
    assert jaccard(fs(), fs()) == 1.0


def test_frequent_itemsets_are_closed() -> None:
    calls = [(fs("a", "b"), 6.0), (fs("a", "b", "c"), 3.0), (fs("d"), 1.0)]
    found = dict(frequent_itemsets(calls, min_support=0.2))
    assert found[fs("a", "b")] == pytest.approx(0.9)
    assert fs("a") not in found and fs("b") not in found  # same support as {a, b}: not closed
    assert found[fs("a", "b", "c")] == pytest.approx(0.3)
    assert fs("d") not in found  # 10 % < 20 %
    assert frequent_itemsets([]) == []


# -- test matching ----------------------------------------------------------------------------------


def test_tests_match_exactly_as_supersets_or_partially() -> None:
    archetype = Archetype(
        "a1",
        fs("email:str", "email:multi-label-domain", "plan:team"),
        fs("email:str"),
        ("email:multi-label-domain",),
        10.0,
        0.2,
        5,
        2,
    )
    tests = {
        "t_exact": fs("email:str", "email:multi-label-domain", "plan:team", "other:int"),
        "t_super": fs("email:str", "email:multi-label-domain", "email:plain", "plan:team"),
        "t_half": fs("email:str", "email:multi-label-domain"),
        "t_common_only": fs("email:str", "email:plain"),
        "t_unrelated": fs("x:int"),
    }
    matches = match_tests(archetype, tests)
    assert [(m.nodeid, m.kind) for m in matches] == [
        ("t_exact", "exact"),
        ("t_super", "superset"),
        ("t_half", "partial"),
    ]
    assert matches[2].overlap == pytest.approx(2 / 3)


def test_strategy_takes_the_most_specific_class_per_parameter() -> None:
    archetype = Archetype(
        "a", fs("email:str", "email:multi-label-domain", "n:zero"), fs(), (), 1.0, 1.0, 1, 1
    )
    classes = {
        "email:str": InputClass("email:str", "type", "a str", "st.text()"),
        "email:multi-label-domain": InputClass(
            "email:multi-label-domain", "email", "sub", 'st.just("a@b.c.d")'
        ),
        "n:zero": InputClass("n:zero", "number", "zero", "st.just(0)"),
    }
    assert archetype_strategy(archetype, classes) == '@given(email=st.just("a@b.c.d"), n=st.just(0))'
    assert archetype_strategy(Archetype("b", fs("q:x"), fs(), (), 1, 1, 1, 1), classes) == ""


# -- the catalogue join and gaps ----------------------------------------------------------------------


def test_classify_shapes_names_classes_like_the_catalogue() -> None:
    classes, info = classify_shapes(
        _region("email"), [("email", PLAIN), ("email", SUBDOMAIN), ("return", PLAIN)]
    )
    assert classes[("email", PLAIN)] == {"email:str", "email:ascii", "email:plain"}
    assert "email:multi-label-domain" in classes[("email", SUBDOMAIN)]
    assert ("return", PLAIN) not in classes
    assert info["email:multi-label-domain"].hypothesis_hint.startswith("st.sampled_from")


def test_gaps_are_classes_production_passed_and_no_test_did() -> None:
    region = _region("email")
    prod = [("email", PLAIN, 70.0, 40), ("email", SUBDOMAIN, 20.0, 12), ("email", TAGGED, 10.0, 6)]
    tests = [("email", SHORT_PLAIN, ("t1",)), ("email", TAGGED, ("t2",)), ("email", EMPTY, ("t3",))]
    result = production_gaps(region, prod, tests, func_calls=1000)
    assert [g.name for g in result.gaps] == ["email:multi-label-domain"]
    gap = result.gaps[0]
    assert gap.share == pytest.approx(0.2)
    assert gap.calls == pytest.approx(200.0)  # scaled to the exact call count
    assert gap.traces == 12
    assert gap.shapes == (SUBDOMAIN,)
    assert "eng.acme.com" in gap.strategy
    assert result.tested and result.prod_calls == 1000
    assert "email:plain" in result.both  # different length bucket, same class: not a gap
    assert result.score == pytest.approx(200.0)


def test_an_untested_function_reports_specific_classes_not_its_type() -> None:
    result = production_gaps(_region("email"), [("email", PLAIN, 5.0, 5)], [])
    assert not result.tested
    assert {g.name for g in result.gaps} == {"email:ascii", "email:plain"}
    assert result.prod_calls == 5.0


def test_a_type_tests_never_pass_is_a_gap_with_a_type_hint() -> None:
    region = _region("domains")
    prod = [("domains", "set|elem:str|len:1", 10.0, 9)]
    tests = [("domains", "list|elem:str|len:1", ("t1",))]
    gaps = {g.name: g for g in production_gaps(region, prod, tests).gaps}
    assert gaps["domains:set"].strategy == "st.sets(st.text())"
    assert gaps["domains:set"].description == "a value of type set"
