"""The economy tables written by ``finalize_run`` for the minimal fixture runs, and their readers.

The fixture bundle (``facts_helpers``) has five tests of ``src/factspkg/calc.py``; ``TEST_SOURCE``
is a test file those nodeids could have come from. Sources and history reach the economy either
through a :class:`FakeHistory` (``EconomyInputs.history``) or through a real bare git mirror beside
the facts store, the way the control plane lays out its data directory.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import textwrap
from collections.abc import Iterable
from pathlib import Path

import pytest
from facts_helpers import (
    CALC_SOURCE,
    MODULE_PATH,
    T_ADD,
    T_BOX,
    T_FAIL,
    T_NEG,
    T_POS,
    TEST_FILE,
    TESTS,
    int_catalogue,
    make_ctx,
    split_bundle,
)

from coverset.analysis.economy import statement_delta
from coverset.analysis.functions import index_functions
from coverset.facts.convert import read_python_bundle
from coverset.facts.economy import (
    ECONOMY_TABLES,
    REMOVABLE_CAVEAT,
    EconomyInputs,
    GitHistory,
    economy_data,
    families,
    func_testing,
    mirror_history,
    pr_test_tax,
    redundancy,
    run_economy,
    tests_economy,
    write_economy,
)
from coverset.facts.ingest import finalize_run, ingest_shard
from coverset.facts.store import FactsStore
from coverset.lang.base import FunctionRegion, NeutralBundle

HEAD = "a" * 40
BASE = "b" * 40
TEST_SOURCE = textwrap.dedent(
    """\
    import pytest

    from factspkg import calc


    def test_add():
        assert calc.add(1, 2) == 3


    @pytest.mark.parametrize("n", [-3, 5], ids=["neg", "pos"])
    def test_classify(n):
        calc.classify(n)


    def test_box_total():
        assert calc.Box([1, 2]).total() == 3


    def test_zero_fails():
        assert calc.classify(0) == "pos"
    """
)
BASE_TEST_SOURCE = textwrap.dedent(
    """\
    import pytest

    from factspkg import calc


    def test_add():
        assert calc.add(1, 2) == 3


    @pytest.mark.parametrize("n", [-3], ids=["neg"])
    def test_classify(n):
        calc.classify(n)
    """
)
OLD_CALC = CALC_SOURCE.replace("    return a + b\n", "    return b + a\n")


class FakeHistory:
    """An in-memory :class:`~coverset.facts.economy.RepoHistory`: trees by commit, last changes by path."""

    def __init__(self, trees: dict[str, dict[str, str]], changes: dict[str, tuple[str, int]]) -> None:
        self.trees = trees
        self.changes = changes
        self.reads: list[tuple[str, tuple[str, ...]]] = []

    def read(self, sha: str, paths: Iterable[str]) -> dict[str, str]:
        wanted = tuple(paths)
        self.reads.append((sha, wanted))
        tree = self.trees.get(sha, {})
        return {p: tree[p] for p in wanted if p in tree}

    def read_at(self, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
        return {(c, p): self.trees[c][p] for c, p in pairs if p in self.trees.get(c, {})}

    def last_changes(self, sha: str, paths: Iterable[str]) -> dict[str, tuple[str, int]]:
        return {p: self.changes[p] for p in paths if p in self.changes}


class BrokenHistory(FakeHistory):
    def read(self, sha: str, paths: Iterable[str]) -> dict[str, str]:
        raise RuntimeError("mirror on fire")


@pytest.fixture
def bundle(minimal_bundle: Path) -> NeutralBundle:
    return read_python_bundle(minimal_bundle)


def _history() -> FakeHistory:
    old = "c" * 40
    return FakeHistory(
        trees={
            HEAD: {MODULE_PATH: CALC_SOURCE, TEST_FILE: TEST_SOURCE},
            BASE: {MODULE_PATH: CALC_SOURCE, TEST_FILE: BASE_TEST_SOURCE},
            old: {MODULE_PATH: OLD_CALC, TEST_FILE: TEST_SOURCE},
        },
        changes={TEST_FILE: (old, 100), MODULE_PATH: ("d" * 40, 200)},
    )


def _finalize(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion], **kwargs: object
) -> None:
    ctx = make_ctx(**kwargs.pop("ctx", {}))  # type: ignore[arg-type]
    ingest_shard(store, ctx, bundle, 0, functions)
    finalize_run(store, ctx, expected_shards=1, catalogue=int_catalogue, **kwargs)  # type: ignore[arg-type]


# -- without any source -----------------------------------------------------------------------------


def test_finalize_writes_the_four_tables(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    _finalize(store, bundle, functions)
    names = {p.name for p in store.run_dir(1, "run-1").iterdir()}
    assert {f"{t}.parquet" for t in ECONOMY_TABLES} <= names
    economy = run_economy(store, 1, "run-1")
    assert economy is not None and economy.totals is not None
    assert economy.totals.tests == 5 and economy.totals.measured_tests == 0
    assert economy.tax is None and economy.weights == {"arc": 1, "class": 3, "line": 1}
    assert any("no git mirror" in note for note in economy.notes)
    assert economy.computed_ms is not None and economy.computed_ms >= 0


def test_redundancy_from_the_tables(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    _finalize(store, bundle, functions)
    found = redundancy(store, 1, "run-1")
    assert found is not None and found.caveat == REMOVABLE_CAVEAT
    # classify(5) runs a subset of what Box([1, 2]).total() runs, and Box is at most 2x slower
    assert found.dominated == ((T_POS, T_BOX),)
    assert [r.nodeid for r in found.removable] == [T_POS]
    assert found.minimal_set_size == 4 and found.tests == 5 and found.groups == ()
    assert found.removable_duration_ms == pytest.approx(30.0)


def test_tests_economy_sorts_filters_and_resolves_ids(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    _finalize(store, bundle, functions)
    rows = tests_economy(store, 1, "run-1", "unique", limit=None)
    assert [r.nodeid for r in rows][0] == T_POS and rows[0].unique_facts == 0
    assert {r.nodeid for r in rows} == set(TESTS)
    assert rows[0].dominated_by == T_BOX and rows[0].family == f"{TEST_FILE}::test_classify"
    assert rows[0].path == TEST_FILE and rows[0].cases == 2 and rows[0].reason == "no sources"
    by_duration = tests_economy(store, 1, "run-1", "duration", limit=2)
    assert [r.nodeid for r in by_duration] == [T_FAIL, T_BOX]
    assert [r.nodeid for r in tests_economy(store, 1, "run-1", only="removable")] == [T_POS]
    assert len(tests_economy(store, 1, "run-1", only="unmeasured", limit=None)) == 5
    assert tests_economy(store, 1, "run-1", path="tests/other.py") == []
    with pytest.raises(ValueError, match="unknown sort"):
        tests_economy(store, 1, "run-1", "fastest")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown filter"):
        tests_economy(store, 1, "run-1", only="flaky")  # type: ignore[arg-type]


def test_families_and_functions_from_the_tables(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    _finalize(store, bundle, functions)
    (family,) = families(store, 1, "run-1")
    assert (family.family, family.cases, family.distinct_flows) == (f"{TEST_FILE}::test_classify", 2, 2)
    assert family.case_ids == (T_NEG, T_POS) and families(store, 1, "run-1", collapsible_only=True) == []
    untested = func_testing(store, 1, "run-1", only="untested")
    assert [f.qualname for f in untested] == ["never_called"]  # excluded() is "pragma: no cover"
    by_name = {f.qualname: f for f in func_testing(store, 1, "run-1")}
    assert by_name["classify"].direct_tests == 3 and by_name["classify"].indirect_tests == 1
    assert by_name["add"].handlers_total is None  # no source, so handlers were not looked for
    with pytest.raises(ValueError, match="unknown filter"):
        func_testing(store, 1, "run-1", only="slow")  # type: ignore[arg-type]


def test_readers_return_nothing_for_a_run_without_economy(store: FactsStore) -> None:
    assert run_economy(store, 1, "run-x") is None and redundancy(store, 1, "run-x") is None
    assert tests_economy(store, 1, "run-x") == [] and families(store, 1, "run-x") == []
    assert func_testing(store, 1, "run-x") == [] and pr_test_tax(store, 1, "run-x") is None


def test_economy_data_matches_the_bundle(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    _finalize(store, bundle, functions)
    data, regions = economy_data(store, 1, "run-1")
    assert data.tests == tuple(sorted(TESTS)) and len(regions) == len(functions)
    expected_rows = sum(len(lines) for by in bundle.coverage.values() for lines in by.values())
    assert data.cov_test.size == expected_rows and (data.cov_test == -1).any()  # <import> kept as -1
    assert data.durations_ms[data.tests.index(T_ADD)] == pytest.approx(10.0)
    assert data.outcomes[data.tests.index(T_FAIL)] == "failed"


# -- with sources and history -------------------------------------------------------------------------


def test_weights_stale_tests_and_the_tax_with_history(store: FactsStore, bundle: NeutralBundle) -> None:
    functions = index_functions(MODULE_PATH, CALC_SOURCE)  # real body hashes, comparable with OLD_CALC's
    base_ctx = make_ctx("run-base", head_sha=BASE, day=1)
    base_bundle = split_bundle(bundle, {T_ADD, T_NEG}, with_import=True)
    ingest_shard(store, base_ctx, base_bundle, 0, functions)
    finalize_run(store, base_ctx, expected_shards=1, economy=EconomyInputs(history=_history()))

    history = _history()
    head_ctx = make_ctx("run-head", head_sha=HEAD, base_sha=BASE, day=2, pr_number=7)
    ingest_shard(store, head_ctx, bundle, 0, functions)
    finalize_run(store, head_ctx, expected_shards=1, economy=EconomyInputs(history=history))

    rows = {r.nodeid: r for r in tests_economy(store, 1, "run-head", limit=None)}
    assert all(r.measured for r in rows.values())
    assert rows[T_ADD].statements == 1 and rows[T_ADD].asserts == 1 and rows[T_ADD].assertion_free is False
    assert rows[T_NEG].assertion_free is True and rows[T_NEG].fixtures == 0
    # add() changed after the test file last did (OLD_CALC); classify and Box did not
    assert rows[T_ADD].stale is True and rows[T_BOX].stale is False and rows[T_NEG].stale is False

    tax = pr_test_tax(store, 1, "run-head")
    assert tax is not None and tax.base_run_id == "run-base" and tax.base_exact and tax.base_sha == BASE
    delta = statement_delta(BASE_TEST_SOURCE, TEST_SOURCE)
    assert delta is not None and tax.added_statements == delta[0] and tax.removed_statements == delta[1]
    head_lines = {line for n in TESTS for line in bundle.coverage[n].get(MODULE_PATH, ())}
    base_lines = {line for n in (T_ADD, T_NEG) for line in bundle.coverage[n].get(MODULE_PATH, ())}
    assert tax.newly_covered_lines == len(head_lines - base_lines)
    assert tax.new_tests == 3 and not tax.flagged and tax.threshold == 8.0
    assert pr_test_tax(store, 1, "run-head", threshold=0.0, min_statements=0).flagged  # type: ignore[union-attr]
    economy = run_economy(store, 1, "run-head")
    assert economy is not None and economy.totals is not None and economy.totals.stale_tests == 1
    assert economy.totals.handlers_total == 0
    # one batch at head (tests, conftests, calc.py), then the helper-module round finds nothing new
    assert (
        history.reads[0][0] == HEAD
        and TEST_FILE in history.reads[0][1]
        and MODULE_PATH in history.reads[0][1]
    )


def test_a_failing_economy_never_fails_the_finalize(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    broken = BrokenHistory({}, {})
    ctx = make_ctx()
    ingest_shard(store, ctx, bundle, 0, functions)
    result = finalize_run(store, ctx, expected_shards=1, economy=EconomyInputs(history=broken))
    assert result.coverage_complete
    economy = run_economy(store, 1, "run-1")
    assert economy is not None and economy.totals is None and economy.tax is None
    assert economy.notes == ("economy failed: RuntimeError: mirror on fire",)
    assert tests_economy(store, 1, "run-1") == []


def test_disabled_economy_writes_nothing(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    ctx = make_ctx()
    ingest_shard(store, ctx, bundle, 0, functions)
    finalize_run(store, ctx, expected_shards=1, economy=EconomyInputs(enabled=False))
    assert run_economy(store, 1, "run-1") is None
    assert write_economy(store, ctx, EconomyInputs(enabled=False)) is None


def test_a_new_ingest_drops_the_economy_until_the_next_finalize(
    store: FactsStore, bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    _finalize(store, bundle, functions)
    assert run_economy(store, 1, "run-1") is not None
    ctx = make_ctx()
    ingest_shard(store, ctx, dataclasses.replace(bundle, meta={"shard": 1}), 1, functions)
    assert run_economy(store, 1, "run-1") is None and tests_economy(store, 1, "run-1") == []


# -- a real git mirror ----------------------------------------------------------------------------------


def _git(cwd: Path, *args: str, date: str = "2026-10-01T12:00:00Z") -> str:
    from coverset.orchestrator.repos import git_env

    env = git_env() | {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True, text=True).stdout


@pytest.fixture
def mirror(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository whose test file was last changed before ``add`` was edited, mirrored bare
    beside a facts store; returns ``(data dir, first commit, head commit)``."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    work = tmp_path / "work"
    (work / "src" / "factspkg").mkdir(parents=True)
    (work / "tests").mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / MODULE_PATH).write_text(OLD_CALC)
    (work / TEST_FILE).write_text(TEST_SOURCE)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "first", date="2026-10-01T12:00:00Z")
    first = _git(work, "rev-parse", "HEAD").strip()
    (work / MODULE_PATH).write_text(CALC_SOURCE)
    _git(work, "commit", "-q", "-am", "edit add", date="2026-10-02T12:00:00Z")
    head = _git(work, "rev-parse", "HEAD").strip()
    data = tmp_path / "data"
    (data / "mirrors").mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(data / "mirrors" / "1.git"))
    return data, first, head


def test_git_history_batches_reads_and_last_changes(mirror: tuple[Path, str, str]) -> None:
    data, first, head = mirror
    history = GitHistory(data / "mirrors" / "1.git")
    changes = history.last_changes(head, [TEST_FILE, MODULE_PATH, "missing.py"])
    assert changes[TEST_FILE][0] == first and changes[MODULE_PATH][0] == head
    assert changes[MODULE_PATH][1] > changes[TEST_FILE][1] and "missing.py" not in changes
    assert history.processes == 1
    found = history.read_at([(first, MODULE_PATH), (head, MODULE_PATH), (head, "missing.py")])
    assert found == {(first, MODULE_PATH): OLD_CALC, (head, MODULE_PATH): CALC_SOURCE}
    assert history.read("not-a-sha", [MODULE_PATH]) == {} and history.last_changes("HEAD", [TEST_FILE]) == {}
    assert history.last_changes(head, [TEST_FILE]) == {TEST_FILE: changes[TEST_FILE]}  # cached
    assert history.processes == 2


def test_the_mirror_beside_the_store_is_used(
    mirror: tuple[Path, str, str], bundle: NeutralBundle, functions: list[FunctionRegion]
) -> None:
    data, _, head = mirror
    store = FactsStore(data / "facts")
    assert mirror_history(store, 1) is not None and mirror_history(store, 2) is None
    regions = index_functions(MODULE_PATH, CALC_SOURCE)
    ctx = make_ctx(head_sha=head)
    ingest_shard(store, ctx, bundle, 0, regions)
    finalize_run(store, ctx, expected_shards=1)
    rows = {r.nodeid: r for r in tests_economy(store, 1, "run-1", limit=None)}
    assert all(r.measured for r in rows.values())
    assert rows[T_ADD].stale is True and rows[T_BOX].stale is False
    economy = run_economy(store, 1, "run-1")
    assert economy is not None and economy.totals is not None and economy.totals.stale_tests == 1
    assert not any("no git mirror" in note for note in economy.notes)
    assert os.path.isdir(data / "mirrors" / "1.git")
