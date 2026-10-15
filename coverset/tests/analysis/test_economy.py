"""The test-economy analysis over synthetic bundles whose answers are worked out by hand.

The main scenario (``_scenario``) has a production module ``src/app.py`` and seven tests:

* ``test_parse_true`` and ``test_parse_true_again`` run the same lines with the same argument shape
  (the second through a helper module): one equivalence group, the slower one a removable candidate;
* ``test_load`` runs a strict subset of ``test_load_and_flag``, which is at most twice as slow:
  dominated (and assertion-free);
* ``test_cases[a|b|c]``: three parametrize cases, two distinct input-class flows;
* ``helper`` is only ever called by ``entry`` (indirectly tested), ``unused`` by nobody, and the
  ``except`` clause of ``load`` never runs.

The PR test tax has its own scenario: the colleague's horror story of a long test file for one
environment-variable parse.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coverset.analysis.cover import csr_of, greedy_cover_csr, prune_csr
from coverset.analysis.economy import (
    CLASS_WEIGHT,
    BaseSnapshot,
    EconomyPolicy,
    EconomyReport,
    analyse,
    base_snapshot,
    conftest_paths,
    family_of,
    find_handlers,
    helper_module_candidates,
    is_test_path,
    parse_nodeid,
    statement_delta,
    tax_message,
    weigh_tests,
)
from coverset.analysis.functions import index_functions
from coverset.lang.base import FunctionRegion, NeutralBundle, ShapeObs, StaticFile, TestRow
from coverset_model.io import read_bundle

APP = "src/app.py"
APP_SOURCE = textwrap.dedent(
    """\
    def parse_flag(value):
        text = value.strip().lower()
        return text in ("1", "true", "yes")


    def load(path):
        try:
            return open(path).read()
        except (FileNotFoundError, PermissionError):
            return None


    def helper(x):
        return x + 1


    def entry(x):
        return helper(x) * 2


    def unused(x):
        return x
    """
)
FLAGS = "tests/test_flags.py"
FLAGS_SOURCE = textwrap.dedent(
    """\
    import pytest
    from app import entry, parse_flag
    from flag_helpers import check_flag


    @pytest.fixture
    def env(monkeypatch):
        monkeypatch.setenv("FLAG", "1")
        return "FLAG"


    def _expect(value, wanted):
        result = parse_flag(value)
        assert result is wanted
        return result


    def test_parse_true(env):
        _expect("true", True)


    def test_parse_true_again():
        check_flag("true")


    @pytest.mark.parametrize("x", [1, 2, -1], ids=["a", "b", "c"])
    def test_cases(x, env):
        assert entry(x) == (x + 1) * 2
    """
)
HELPERS = "tests/flag_helpers.py"
HELPERS_SOURCE = textwrap.dedent(
    """\
    from app import parse_flag


    def check_flag(value):
        assert parse_flag(value)
        _log(value)


    def _log(value):
        print(value)
        return value
    """
)
CONFTEST = "tests/conftest.py"
CONFTEST_SOURCE = textwrap.dedent(
    """\
    import pytest


    @pytest.fixture(autouse=True)
    def clean_env(monkeypatch):
        monkeypatch.delenv("FLAG", raising=False)
        yield
    """
)
LOAD = "tests/test_load.py"
LOAD_SOURCE = textwrap.dedent(
    """\
    from app import load, parse_flag


    def test_load(tmp_path):
        load(tmp_path / "x")


    def test_load_and_flag(tmp_path):
        assert load(tmp_path / "x") is None
        assert parse_flag(" TRUE ")
    """
)
SOURCES = {
    APP: APP_SOURCE,
    FLAGS: FLAGS_SOURCE,
    HELPERS: HELPERS_SOURCE,
    CONFTEST: CONFTEST_SOURCE,
    LOAD: LOAD_SOURCE,
}

T_A = f"{FLAGS}::test_parse_true"
T_B = f"{FLAGS}::test_parse_true_again"
T_C = f"{LOAD}::test_load"
T_D = f"{LOAD}::test_load_and_flag"
CASE_A, CASE_B, CASE_C = (f"{FLAGS}::test_cases[{c}]" for c in "abc")
FAMILY = f"{FLAGS}::test_cases"

FUNCS = {r.qualname: r for r in index_functions(APP, APP_SOURCE) if r.kind == "function"}


def _obs(func: str, param: str, shape: str, caller: str | tuple[str, str, int], *tests: str) -> ShapeObs:
    return ShapeObs(FUNCS[func].triple, param, shape, caller, len(tests), tests, False, None, None)


def _bundle(
    coverage: dict[str, dict[str, tuple[int, ...]]],
    durations: dict[str, float],
    shapes: tuple[ShapeObs, ...] = (),
    *,
    static: dict[str, StaticFile] | None = None,
    run_arcs: dict[str, tuple[tuple[int, int], ...]] | None = None,
    outcomes: dict[str, str] | None = None,
) -> NeutralBundle:
    tests = tuple(
        TestRow(n, n.split("::")[0], (outcomes or {}).get(n, "passed"), d, None, None, None, None, None)  # type: ignore[arg-type]
        for n, d in durations.items()
    )
    return NeutralBundle(
        meta={},
        collected=(),
        tests=tests,
        coverage=coverage,
        static=static or {},
        run_arcs=run_arcs or {},
        shapes=shapes,
        sampling=(),
        durations={},
    )


def _scenario() -> NeutralBundle:
    entry_caller = FUNCS["entry"].triple
    statements = (2, 3, 7, 8, 9, 10, 14, 18, 22)
    return _bundle(
        coverage={
            T_A: {APP: (2, 3)},
            T_B: {APP: (2, 3)},
            T_C: {APP: (7, 8)},
            T_D: {APP: (2, 3, 7, 8)},
            CASE_A: {APP: (14, 18)},
            CASE_B: {APP: (14, 18)},
            CASE_C: {APP: (14, 18)},
        },
        durations={
            T_A: 0.010,
            T_B: 0.020,
            T_C: 0.010,
            T_D: 0.015,
            CASE_A: 0.010,
            CASE_B: 0.010,
            CASE_C: 0.010,
        },
        shapes=(
            _obs("parse_flag", "value", "str|ascii|len:2-10", T_A, T_A),
            _obs("parse_flag", "value", "str|ascii|len:2-10", (HELPERS, "check_flag", 4), T_B),
            _obs("entry", "x", "int|pos", CASE_A, CASE_A),
            _obs("entry", "x", "int|pos", CASE_B, CASE_B),
            _obs("entry", "x", "int|neg", CASE_C, CASE_C),
            _obs("helper", "x", "int|pos", entry_caller, CASE_A, CASE_B),
            _obs("helper", "x", "int|zero", entry_caller, CASE_C),
        ),
        static={APP: StaticFile(APP, 22, statements, (), ())},
    )


@pytest.fixture(scope="module")
def report() -> EconomyReport:
    return analyse(_scenario(), SOURCES)


# -- redundancy -----------------------------------------------------------------------------------


def test_a_redundant_pair_is_one_group_and_the_slower_one_is_a_candidate(report: EconomyReport) -> None:
    tests = report.by_nodeid()
    assert tests[T_A].signature == tests[T_B].signature
    assert tests[T_A].group_id is not None and tests[T_A].group_id == tests[T_B].group_id
    assert tests[T_A].in_minimal_set and not tests[T_A].removable
    assert not tests[T_B].in_minimal_set and tests[T_B].removable
    assert report.groups() == {tests[T_A].group_id: [T_A, T_B], tests[CASE_A].group_id: [CASE_A, CASE_B]}
    assert report.totals.equivalence_groups == 2 and report.totals.tests_in_groups == 4


def test_a_strict_subset_of_a_test_at_most_twice_as_slow_is_dominated(report: EconomyReport) -> None:
    tests = report.by_nodeid()
    assert tests[T_C].dominated_by == T_D
    assert all(t.dominated_by is None for t in report.tests if t.nodeid != T_C)
    assert report.totals.dominated == 1


def test_dominance_respects_the_slowdown_limit() -> None:
    bundle = _scenario()
    slow = dict(bundle.durations) | {T_C: 0.010, T_D: 0.025}  # 2.5x slower: not a dominator
    report = analyse(bundle, SOURCES, durations=slow)
    assert report.by_nodeid()[T_C].dominated_by is None
    looser = analyse(bundle, SOURCES, durations=slow, policy=EconomyPolicy(dominance_slowdown=3.0))
    assert looser.by_nodeid()[T_C].dominated_by == T_D


def test_the_minimal_set_is_worked_out_by_hand(report: EconomyReport) -> None:
    minimal = {t.nodeid for t in report.tests if t.in_minimal_set}
    assert minimal == {CASE_A, CASE_C, T_A, T_C}
    assert {t.nodeid for t in report.removable()} == {T_B, T_D, CASE_B}
    assert report.removable()[0].nodeid == T_B  # slowest first
    assert report.totals.minimal_set_size == 4 and report.totals.removable_candidates == 3
    assert report.totals.removable_duration_ms == pytest.approx(45.0)


def test_skipped_and_empty_tests_are_never_removable() -> None:
    bundle = _bundle(
        coverage={"tests/test_x.py::test_a": {APP: (2,)}, "tests/test_x.py::test_b": {APP: (2,)}},
        durations={
            "tests/test_x.py::test_a": 0.01,
            "tests/test_x.py::test_b": 0.02,
            "tests/test_x.py::test_c": 0.0,
        },
        outcomes={"tests/test_x.py::test_c": "skipped"},
    )
    tests = analyse(bundle).by_nodeid()
    assert tests["tests/test_x.py::test_b"].removable
    assert tests["tests/test_x.py::test_c"].empty and not tests["tests/test_x.py::test_c"].removable
    assert tests["tests/test_x.py::test_c"].signature == tests["tests/test_x.py::test_c"].signature
    assert tests["tests/test_x.py::test_c"].group_id is None


# -- families -------------------------------------------------------------------------------------


def test_a_family_of_three_cases_reaches_two_flows(report: EconomyReport) -> None:
    (family,) = report.families
    assert family.family == FAMILY and family.cases == 3
    assert family.distinct_flows == 2 and family.distinct_signatures == 2
    assert family.redundant_cases == 1 and family.case_ids == (CASE_A, CASE_B, CASE_C)
    assert report.by_nodeid()[CASE_B].cases == 3 and report.by_nodeid()[T_A].cases == 1
    assert report.totals.collapsible_families == 1 and report.totals.redundant_cases == 1


def test_a_family_without_observations_counts_signatures_only() -> None:
    nodeids = [f"tests/test_x.py::test_f[{i}]" for i in range(3)]
    bundle = _bundle(
        coverage={nodeids[0]: {APP: (2,)}, nodeids[1]: {APP: (2,)}, nodeids[2]: {APP: (3,)}},
        durations=dict.fromkeys(nodeids, 0.01),
    )
    (family,) = analyse(bundle).families
    assert family.distinct_flows is None and family.distinct_signatures == 2 and family.redundant_cases == 1


# -- weights and the coverage value ---------------------------------------------------------------


def test_weights_count_statements_asserts_fixtures_and_helpers(report: EconomyReport) -> None:
    tests = report.by_nodeid()
    a = tests[T_A].weight
    # env (2) + autouse clean_env (2) + _expect (3); _expect's assert is a helper assert
    assert (a.statements, a.asserts, a.helper_asserts, a.fixtures, a.helper_statements) == (1, 0, 1, 2, 7)
    assert a.helpers == (f"{CONFTEST}::clean_env", f"{FLAGS}::_expect", f"{FLAGS}::env")
    b = tests[T_B].weight
    # check_flag (2) and, at depth 2, _log (2) from the same-directory helper module; clean_env (2)
    assert (b.statements, b.helper_asserts, b.fixtures, b.helper_statements) == (1, 1, 1, 6)
    assert f"{HELPERS}::_log" in b.helpers
    case = tests[CASE_A].weight  # the parametrize argument is not a fixture
    assert (case.statements, case.asserts, case.fixtures, case.helper_statements) == (1, 1, 2, 4)
    assert tests[T_D].weight.asserts == 2 and tests[T_D].weight.file_lines == len(LOAD_SOURCE.splitlines())


def test_assertion_free_tests_are_flagged(report: EconomyReport) -> None:
    tests = report.by_nodeid()
    assert tests[T_C].assertion_free is True
    assert tests[T_A].assertion_free is False  # its helper asserts
    assert tests[T_D].assertion_free is False
    assert report.totals.assertion_free == 1


def test_unique_facts_and_leverage(report: EconomyReport) -> None:
    case_c = report.by_nodeid()[CASE_C]
    # entry x:negative and helper x:zero are its own input classes; everything else is shared
    assert (case_c.unique_lines, case_c.unique_arcs, case_c.unique_classes) == (0, 0, 2)
    assert case_c.unique_facts == 2 * CLASS_WEIGHT
    assert case_c.leverage == pytest.approx(6 / 5)  # one own statement plus four helper statements
    assert case_c.cost_per_fact_ms == pytest.approx(10.0 / 6, abs=1e-4)
    assert report.by_nodeid()[T_A].unique_facts == 0 and report.by_nodeid()[T_A].cost_per_fact_ms is None
    assert report.totals.lines == 6 and report.totals.observations == 5


def test_without_sources_weights_are_not_measured_never_zero() -> None:
    report = analyse(_scenario())
    test = report.by_nodeid()[T_A]
    assert not test.measured and test.weight.statements is None and test.leverage is None
    assert test.assertion_free is None and test.weight.reason == "no sources"
    assert any("test weights not measured" in note for note in report.notes)
    assert report.totals.handlers_total is None


def test_arcs_are_inferred_from_both_ends() -> None:
    nodeids = ["tests/test_x.py::test_true", "tests/test_x.py::test_false", "tests/test_x.py::test_both"]
    bundle = _bundle(
        coverage={nodeids[0]: {APP: (2, 3)}, nodeids[1]: {APP: (2, 5)}, nodeids[2]: {APP: (2, 3, 5)}},
        durations=dict.fromkeys(nodeids, 0.01),
        run_arcs={APP: ((2, 3), (2, 5), (3, -1))},
    )
    tests = analyse(bundle).by_nodeid()
    assert tests[nodeids[0]].arcs == 2  # 2->3 and the exit 3->-1
    assert tests[nodeids[1]].arcs == 1
    assert tests[nodeids[2]].arcs == 3  # both ends of both arcs: over-attributed, as documented


# -- functions ------------------------------------------------------------------------------------


def test_direct_indirect_and_untested_functions(report: EconomyReport) -> None:
    by_name = {f.qualname: f for f in report.functions}
    assert by_name["helper"].indirect_only and by_name["helper"].indirect_tests == 3
    assert by_name["entry"].direct_tests == 3 and not by_name["entry"].indirect_only
    assert by_name["parse_flag"].direct_tests == 2  # a test helper calling it counts as direct
    assert by_name["unused"].untested and not by_name["unused"].covered
    assert by_name["load"].covered and not by_name["load"].observed and not by_name["load"].untested
    assert report.totals.indirect_only_functions == 1 and report.totals.untested_functions == 1


def test_an_unexercised_handler_is_reported_with_its_types(report: EconomyReport) -> None:
    load = next(f for f in report.functions if f.qualname == "load")
    assert (load.handlers_total, load.handlers_unexercised) == (1, 1)
    (handler,) = load.handlers
    assert (handler.line, handler.body_line) == (9, 10)
    assert handler.types == ("FileNotFoundError", "PermissionError") and handler.executed is False
    assert report.totals.handlers_unexercised == 1


def test_a_handler_run_by_import_or_a_test_is_exercised() -> None:
    bundle = _scenario()
    coverage = dict(bundle.coverage) | {"<import>": {APP: (10,)}}
    report = analyse(NeutralBundle(**{**_fields(bundle), "coverage": coverage}), SOURCES)
    load = next(f for f in report.functions if f.qualname == "load")
    assert load.handlers_unexercised == 0 and load.handlers == ()


def test_excluded_functions_and_handlers_are_left_out() -> None:
    bundle = _scenario()
    static = {APP: StaticFile(APP, 22, (2, 3, 7, 8, 9, 14, 18), (10, 21, 22), ())}
    report = analyse(NeutralBundle(**{**_fields(bundle), "static": static}), SOURCES)
    names = {f.qualname for f in report.functions}
    assert "unused" not in names
    assert next(f for f in report.functions if f.qualname == "load").handlers_total == 0


def test_find_handlers_names_bare_and_star_excepts() -> None:
    source = "try:\n    x = 1\nexcept:\n    pass\ntry:\n    y = 2\nexcept* ValueError:\n    z = 3\n"
    assert [(h.line, h.body_line, h.types) for h in find_handlers("m.py", source)] == [
        (3, 4, ("BaseException",)),
        (7, 8, ("ValueError",)),
    ]
    assert find_handlers("m.py", "def (") == []


# -- stale tests ----------------------------------------------------------------------------------


def test_stale_tests_need_every_covered_function_changed() -> None:
    seen: dict[str, list[str]] = {}

    def lookup(requests: dict[str, list[FunctionRegion]]) -> dict[str, dict[str, str | None] | None]:
        seen.update({path: [r.qualname for r in regions] for path, regions in requests.items()})
        return {
            FLAGS: {r.func_key: "old" for r in requests[FLAGS]},  # everything changed since
            LOAD: {
                FUNCS["load"].func_key: "old",
                FUNCS["parse_flag"].func_key: FUNCS["parse_flag"].body_hash,
            },
        }

    report = analyse(_scenario(), SOURCES, stale_lookup=lookup)
    tests = report.by_nodeid()
    assert seen == {FLAGS: ["entry", "helper", "parse_flag"], LOAD: ["load", "parse_flag"]}
    assert tests[T_A].stale and tests[CASE_A].stale
    assert tests[T_C].stale  # it covers only load, which changed
    assert tests[T_D].stale is False  # parse_flag did not change
    assert report.totals.stale_tests == 6  # the five in test_flags.py and test_load


def test_stale_is_unknown_without_history() -> None:
    report = analyse(_scenario(), SOURCES, stale_lookup=lambda requests: dict.fromkeys(requests))
    assert all(t.stale is None for t in report.tests) and report.totals.stale_tests is None
    assert any("stale tests not measured" in note for note in report.notes)


# -- the PR test tax ------------------------------------------------------------------------------

SETTINGS = "src/settings.py"
SETTINGS_SOURCE = textwrap.dedent(
    """\
    import os


    def env_flag(name):
        raw = os.environ.get(name, "")
        text = raw.strip().lower()
        return text in ("1", "true")
    """
)
ENV_TEST = "tests/test_settings.py"
ENV_TEST_SOURCE = textwrap.dedent(
    '''\
    import os

    import pytest

    from settings import env_flag

    TRUTHY = ["true", "1", "TRUE"]


    class EnvSandbox:
        """Remembers and restores one environment variable."""

        def __init__(self, name):
            self.name = name
            self.saved = os.environ.get(name)

        def set(self, value):
            os.environ[self.name] = value
            return value

        def clear(self):
            os.environ.pop(self.name, None)

        def restore(self):
            if self.saved is None:
                self.clear()
            else:
                os.environ[self.name] = self.saved


    @pytest.fixture
    def sandbox():
        box = EnvSandbox("FEATURE_X")
        yield box
        box.restore()


    def _set_and_read(sandbox, value):
        sandbox.set(value)
        result = env_flag(sandbox.name)
        assert isinstance(result, bool)
        return result


    @pytest.mark.parametrize("value", TRUTHY)
    def test_truthy_values(sandbox, value):
        result = _set_and_read(sandbox, value)
        assert result is True
        assert env_flag("FEATURE_X") == result
        sandbox.clear()
        assert env_flag("FEATURE_X") is False


    def test_true_lowercase(sandbox):
        sandbox.set("true")
        value = env_flag("FEATURE_X")
        assert value
        assert value is True
        assert str(value) == "True"


    def test_true_one(sandbox):
        sandbox.set("1")
        value = env_flag("FEATURE_X")
        assert value
        assert value is True
        assert str(value) == "True"


    def test_true_upper(sandbox):
        sandbox.set("TRUE")
        value = env_flag("FEATURE_X")
        assert value
        assert value is True
        assert str(value) == "True"
    '''
)
ENV_FLAG = next(r for r in index_functions(SETTINGS, SETTINGS_SOURCE) if r.qualname == "env_flag")


def _tax_bundle() -> NeutralBundle:
    nodeids = [f"{ENV_TEST}::test_truthy_values[{v}]" for v in ("true", "1", "TRUE")]
    nodeids += [f"{ENV_TEST}::test_true_{x}" for x in ("lowercase", "one", "upper")]
    shapes = (
        ShapeObs(
            ENV_FLAG.triple, "name", "str|ascii|len:2-10", nodeids[0], 6, tuple(nodeids), False, None, None
        ),
    )
    return _bundle(
        coverage={n: {SETTINGS: (5, 6, 7), APP: (2, 3)} for n in nodeids},
        durations=dict.fromkeys(nodeids, 0.002),
        shapes=shapes,
        static={
            SETTINGS: StaticFile(SETTINGS, 7, (1, 4, 5, 6, 7), (), ()),
            APP: StaticFile(APP, 22, (2, 3), (), ()),
        },
    )


def _tax_base(**overrides: object) -> BaseSnapshot:
    values: dict[str, object] = {
        "run_id": "run-base",
        "sha": "b" * 40,
        "exact": True,
        "coverage": {APP: frozenset({2, 3})},
        "statements": {APP: frozenset({2, 3})},
        "classes": frozenset({(ENV_FLAG.func_key, "name:str")}),
        "tests": frozenset(),
        "test_sources": {},
        "sources": {APP: APP_SOURCE},
    }
    return BaseSnapshot(**(values | overrides))  # type: ignore[arg-type]


def test_the_horror_story_is_flagged_with_its_message() -> None:
    sources = {SETTINGS: SETTINGS_SOURCE, ENV_TEST: ENV_TEST_SOURCE, APP: APP_SOURCE}
    report = analyse(_tax_bundle(), sources, base=_tax_base())
    tax = report.tax
    assert tax is not None
    added = statement_delta(None, ENV_TEST_SOURCE)
    assert added is not None and added[0] >= 50
    assert tax.added_statements == added[0] and tax.removed_statements == 0 and tax.test_files_changed == 1
    assert tax.newly_covered_lines == 3  # settings.py 5-7; app.py 2-3 were covered in base
    assert tax.new_classes == 1  # name:ascii (name:str was observed in base)
    assert tax.new_tests == 6 and tax.base_exact
    assert tax.tax == pytest.approx(round(added[0] / (3 + CLASS_WEIGHT), 3))
    assert tax.flagged and tax.threshold == 8.0
    assert tax.message == f"{added[0]} test statements added for 3 newly covered lines and 1 input class"


def test_lines_are_matched_across_the_diff() -> None:
    sources = {SETTINGS: SETTINGS_SOURCE, ENV_TEST: ENV_TEST_SOURCE, APP: APP_SOURCE}
    shifted = "# a new comment\n# and another\n" + APP_SOURCE
    base = _tax_base(sources={APP: shifted}, coverage={APP: frozenset({4, 5})})
    # head lines 2-3 are base lines 4-5 (the base had two more lines on top): still covered before
    assert analyse(_tax_bundle(), sources, base=base).tax.newly_covered_lines == 3  # type: ignore[union-attr]
    unmatched = _tax_base(sources={}, coverage={APP: frozenset({4, 5})})
    assert analyse(_tax_bundle(), sources, base=unmatched).tax.newly_covered_lines == 5  # type: ignore[union-attr]


def test_a_small_test_addition_is_never_flagged() -> None:
    small = "from settings import env_flag\n\n\ndef test_true_upper():\n    assert env_flag('X') is False\n"
    sources = {SETTINGS: SETTINGS_SOURCE, ENV_TEST: small, APP: APP_SOURCE}
    tax = analyse(_tax_bundle(), sources, base=_tax_base()).tax
    assert tax is not None and tax.added_statements == 3 and not tax.flagged
    strict = analyse(
        _tax_bundle(),
        sources,
        base=_tax_base(),
        policy=EconomyPolicy(test_tax_min_statements=1, test_tax_threshold=0.1),
    )
    assert strict.tax is not None and strict.tax.flagged


def test_without_a_base_there_is_no_tax(report: EconomyReport) -> None:
    assert report.tax is None and any("PR test tax not computed" in note for note in report.notes)


def test_base_snapshot_of_a_bundle() -> None:
    snapshot = base_snapshot(_scenario(), run_id="r", test_sources={FLAGS: FLAGS_SOURCE})
    assert snapshot.coverage[APP] == frozenset({2, 3, 7, 8, 14, 18})
    assert (FUNCS["entry"].func_key, "x:negative") in snapshot.classes
    assert T_A in snapshot.tests and snapshot.test_sources == {FLAGS: FLAGS_SOURCE}


def test_tax_message_pluralises() -> None:
    assert tax_message(1, 1, 1) == "1 test statement added for 1 newly covered line and 1 input class"
    assert (
        tax_message(1200, 0, 2) == "1,200 test statements added for 0 newly covered lines and 2 input classes"
    )


# -- statements, ids and paths --------------------------------------------------------------------


def test_statement_delta_ignores_formatting_and_moves_but_counts_copies() -> None:
    base = "def test_a():\n    x = 1\n    assert x\n\n\ndef test_b():\n    assert True\n"
    reformatted = "def test_a():\n    x=1\n    assert (x)\n\ndef test_b():\n    assert True\n"
    moved = "def test_b():\n    assert True\n\n\ndef test_a():\n    x = 1\n    assert x\n"
    copied = base + "\n\ndef test_c():\n    x = 1\n    assert x\n"
    assert statement_delta(base, reformatted) == (0, 0)
    assert statement_delta(base, moved) == (0, 0)
    assert statement_delta(base, copied) == (3, 0)  # def test_c, x = 1, assert x
    assert statement_delta(copied, base) == (0, 3)
    assert statement_delta(None, '"""Only a docstring."""\n') == (0, 0)
    assert statement_delta("def (", base) is None


def test_parse_nodeid_and_family() -> None:
    ref = parse_nodeid("tests/test_a.py::TestX::Inner::test_y[a::b-1]")
    assert ref is not None and (ref.path, ref.classes, ref.function, ref.params) == (
        "tests/test_a.py",
        ("TestX", "Inner"),
        "test_y",
        "a::b-1",
    )
    assert parse_nodeid("src/a.test.ts > suite > works") is None
    assert parse_nodeid("t.py::test_y", "pkg/t.py").path == "pkg/t.py"  # type: ignore[union-attr]
    assert family_of("t.py::test_y[1]") == "t.py::test_y" and family_of("t.py::test_y") is None


def test_test_paths_and_conftests() -> None:
    assert is_test_path("tests/helpers.py") and is_test_path("pkg/test_a.py") and is_test_path("a/b_test.py")
    assert is_test_path("web/src/app.spec.ts") and is_test_path("src/conftest.py")
    assert not is_test_path("src/app.py") and is_test_path("src/app.py", {"src/app.py"})
    assert conftest_paths("a/b/test_x.py") == ["a/b/conftest.py", "a/conftest.py", "conftest.py"]
    assert helper_module_candidates(FLAGS, FLAGS_SOURCE) == ["tests/app.py", HELPERS]  # test code only


def test_unittest_and_mock_assertions_count() -> None:
    source = textwrap.dedent(
        """\
        import unittest
        from pytest import raises


        class Base(unittest.TestCase):
            def check(self, value):
                self.assertTrue(value)


        class TestThing(Base):
            def test_it(self):
                mock = object()
                mock.assert_called_once_with(1)
                self.assertEqual(1, 1)
                self.check(True)

            def test_raises(self):
                with raises(ValueError):
                    int("x")

            def test_nothing(self):
                print("hi")
        """
    )
    weights = weigh_tests(
        {
            f"tests/test_u.py::TestThing::{n}": "tests/test_u.py"
            for n in ("test_it", "test_raises", "test_nothing")
        },
        {"tests/test_u.py": source},
    )
    it = weights["tests/test_u.py::TestThing::test_it"]
    assert (it.statements, it.asserts, it.helper_asserts, it.helper_statements) == (4, 2, 1, 1)
    raises_ = weights["tests/test_u.py::TestThing::test_raises"]
    assert raises_.raises and raises_.asserts == 1
    nothing = weights["tests/test_u.py::TestThing::test_nothing"]
    assert nothing.asserts == 0 and not nothing.raises
    missing = weigh_tests({"tests/test_u.py::test_gone": "tests/test_u.py"}, {"tests/test_u.py": source})
    assert not missing["tests/test_u.py::test_gone"].measured
    assert missing["tests/test_u.py::test_gone"].reason == "test function not found"


# -- the covering set, as a property ---------------------------------------------------------------

_SETS = st.dictionaries(
    st.integers(0, 40).map(lambda i: f"t{i:03d}"),
    st.frozensets(st.integers(0, 60), max_size=12),
    min_size=1,
    max_size=25,
)


@settings(max_examples=150, deadline=None)
@given(_SETS, st.data())
def test_the_minimal_set_covers_every_fact_and_is_irredundant(
    sets: dict[str, frozenset[int]], data: st.DataObject
) -> None:
    keys = sorted(sets)
    weights = {k: data.draw(st.floats(0.0005, 5.0), label=k) for k in keys}
    off, el = csr_of([sets[k] for k in keys])
    w = np.array([weights[k] for k in keys])
    chosen = prune_csr(greedy_cover_csr(off, el, w), off, el, w)
    picked = [keys[i] for i in chosen]
    universe = set().union(*sets.values())
    assert set().union(*(sets[k] for k in picked)) == universe if picked else not universe
    for k in picked:  # irredundant: no chosen set can be dropped
        others = set().union(*(sets[o] for o in picked if o != k))
        assert not sets[k] <= others


@settings(max_examples=60, deadline=None)
@given(st.lists(st.frozensets(st.integers(1, 30), min_size=1, max_size=8), min_size=1, max_size=12))
def test_removing_every_candidate_keeps_every_line(line_sets: list[frozenset[int]]) -> None:
    nodeids = [f"tests/test_p.py::test_{i:02d}" for i in range(len(line_sets))]
    bundle = _bundle(
        coverage={n: {APP: tuple(sorted(s))} for n, s in zip(nodeids, line_sets, strict=True)},
        durations={n: 0.001 * (i % 5 + 1) for i, n in enumerate(nodeids)},
    )
    report = analyse(bundle)
    kept = [t.nodeid for t in report.tests if t.in_minimal_set]
    assert set().union(*(line_sets[nodeids.index(n)] for n in kept)) == set().union(*line_sets)
    assert {t.nodeid for t in report.tests if t.removable} == set(nodeids) - set(kept)


def _fields(bundle: NeutralBundle) -> dict[str, object]:
    return {name: getattr(bundle, name) for name in bundle.__dataclass_fields__}


# -- other languages and edge cases ---------------------------------------------------------------


def test_a_typescript_bundle_gets_coverage_value_but_no_python_weights() -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "bundles" / "neutral-ts"
    bundle = read_bundle(fixture / "bundle")
    root = fixture / "repo"
    report = analyse(bundle, lambda path: (root / path).read_text() if (root / path).is_file() else None)
    assert report.totals.tests == len(bundle.tests) and report.totals.measured_tests == 0
    assert all(t.weight.reason == "not a pytest test id" for t in report.tests)
    assert (
        report.totals.lines > 0
        and report.totals.classes > 0
        and report.totals.functions == len(bundle.functions)
    )
    assert report.totals.handlers_total is None  # handlers are read from Python sources only
    assert any("not a pytest test id" in note for note in report.notes)
    assert not any("exception handlers" in note for note in report.notes)


def test_an_empty_bundle_is_all_zeros() -> None:
    report = analyse(NeutralBundle({}, (), (), {}, {}, {}, (), (), {}), {})
    assert report.tests == () and report.families == () and report.functions == ()
    assert report.totals.tests == 0 and report.totals.minimal_set_size == 0 and report.totals.lines == 0
