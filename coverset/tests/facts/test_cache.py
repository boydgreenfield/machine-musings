"""The facts caches: run stamps, leased connections, memoised results, the per-run summaries and
the web's indexed run facts (``coverset.facts.cache``, ``facts.aggregates``, ``web.loaders``).

Every cache is keyed by run stamps, so each test checks both halves: a second read is a hit, and an
ingest, a finalize or a replaced file is seen by the very next read. The summaries are a cache of
``test_lines`` too: whatever they answer must equal what ``test_lines`` answers.
"""

from __future__ import annotations

import dataclasses
import os
import threading
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
from facts_helpers import MODULE_PATH, T_BOX, T_NEG, int_catalogue, make_ctx, split_bundle

from coverset.facts import cache, queries
from coverset.facts.atomic import atomic_write
from coverset.facts.convert import read_python_bundle
from coverset.facts.ingest import finalize_run, ingest_shard
from coverset.facts.schema import SUMMARY_TABLES
from coverset.facts.store import FactsStore
from coverset.lang.base import FunctionRegion
from coverset.web.facts_view import RunFacts
from coverset.web.loaders import IndexedRunFacts, cached_run_facts, clear_caches


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    """Every test starts with empty process caches (they are keyed by store root anyway)."""
    cache.CONNECTIONS.clear()
    cache.RESULTS.clear()
    clear_caches()


@pytest.fixture
def facts(store: FactsStore, minimal_bundle: Path, functions: list[FunctionRegion]) -> FactsStore:
    """run-1 finalized (with summaries), run-2 ingested from two of its tests and not finalized."""
    bundle = read_python_bundle(minimal_bundle)
    ctx = make_ctx("run-1", day=1)
    ingest_shard(store, ctx, bundle, 0, functions)
    finalize_run(store, ctx, expected_shards=1, catalogue=int_catalogue)
    later = split_bundle(bundle, {T_NEG, T_BOX}, with_import=True)
    ingest_shard(store, make_ctx("run-2", day=2, head_sha="c" * 40), later, 0, functions)
    return store


# -- stamps ----------------------------------------------------------------------------------------


def test_a_replaced_file_changes_the_stamp_even_with_the_same_size_and_mtime(tmp_path: Path) -> None:
    run = tmp_path / "run=r"
    run.mkdir()
    target = run / "file_cov.parquet"
    atomic_write(target, lambda tmp: tmp.write_bytes(b"x" * 64))
    before = cache.run_stamp(run)
    st = target.stat()
    atomic_write(target, lambda tmp: tmp.write_bytes(b"y" * 64))
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # a coarse clock: same tick
    after = cache.run_stamp(run)
    assert before != after and before[0][2:] == after[0][2:]  # same size and mtime, new inode
    (run / ".file_cov.parquet.123.tmp").write_bytes(b"in progress")
    (run / ".lock").touch()
    assert cache.run_stamp(run) == after  # hidden files are never part of it
    assert cache.run_stamp(tmp_path / "missing") == ()


# -- leased connections ------------------------------------------------------------------------------


def test_a_lease_reuses_the_scope_connection_until_the_run_changes(
    facts: FactsStore, minimal_bundle: Path, functions: list[FunctionRegion]
) -> None:
    def tests_in_run_2() -> int:
        with facts.lease(repo_id=1, run_id="run-2", tables=("tests_dim",)) as con:
            return int(con.execute("SELECT count(DISTINCT nodeid) FROM tests_dim").fetchone()[0])

    first = tests_in_run_2()
    assert tests_in_run_2() == first
    assert (cache.CONNECTIONS.stats.misses, cache.CONNECTIONS.stats.hits) == (1, 1)
    # Ingesting another shard commits new files: the next lease sees them.
    bundle = read_python_bundle(minimal_bundle)
    ingest_shard(facts, make_ctx("run-2", day=2, head_sha="c" * 40), bundle, 1, functions)
    assert tests_in_run_2() > first
    assert cache.CONNECTIONS.stats.invalidations == 1


def test_a_finalize_is_seen_by_the_next_lease(facts: FactsStore) -> None:
    def file_cov_rows() -> int:
        with facts.lease(repo_id=1, run_id="run-2", tables=("file_cov",)) as con:
            return int(con.execute("SELECT count(*) FROM file_cov").fetchone()[0])

    assert file_cov_rows() == 0
    finalize_run(facts, make_ctx("run-2", day=2, head_sha="c" * 40), expected_shards=1)
    assert file_cov_rows() > 0


def test_the_connection_cache_is_bounded_and_least_recently_used_goes_first() -> None:
    built: list[str] = []

    def build(name: str) -> duckdb.DuckDBPyConnection:
        built.append(name)
        return duckdb.connect()

    lru = cache.ConnectionCache(max_entries=2)
    for key in ("a", "b", "a", "c", "a", "b"):
        lru.lease(key, 0, lambda key=key: build(key)).close()
    assert built == ["a", "b", "c", "b"]  # "b" was evicted by "c", "a" stayed (recently used)
    assert len(lru) == 2 and lru.stats.evictions == 2


def test_a_cursor_outlives_the_eviction_of_its_connection() -> None:
    lru = cache.ConnectionCache(max_entries=1)
    held = [duckdb.connect()]
    held[0].execute("CREATE VIEW v AS SELECT 42 AS x")
    cursor = lru.lease("a", 0, held.pop)
    lru.lease("b", 0, duckdb.connect).close()  # evicts "a": dropped, never closed
    assert cursor.execute("SELECT x FROM v").fetchone() == (42,)
    cursor.close()


def test_an_arrow_table_registered_on_a_lease_is_private_to_it(facts: FactsStore) -> None:
    with facts.lease(repo_id=1, run_id="run-1", tables=("files",)) as one:
        one.register("changed", pa.table({"path": ["x"]}))
        assert one.execute("SELECT count(*) FROM changed").fetchone() == (1,)
        with (
            facts.lease(repo_id=1, run_id="run-1", tables=("files",)) as other,
            pytest.raises(duckdb.CatalogException),
        ):
            other.execute("SELECT count(*) FROM changed")


def test_leases_from_many_threads_share_one_connection(facts: FactsStore) -> None:
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(20):
                assert queries.tests_covering(facts, 1, "run-1", MODULE_PATH, 1, 100)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert cache.CONNECTIONS.stats.misses <= 8  # built at most once per racing thread


@pytest.mark.security
def test_a_scoped_connection_cannot_read_another_repository(
    store: FactsStore, minimal_bundle: Path, functions: list[FunctionRegion]
) -> None:
    bundle = read_python_bundle(minimal_bundle)
    for repo in (1, 2):
        ingest_shard(store, make_ctx("run-1", repo_id=repo), bundle, 0, functions)
    other = store.table_files(2, "run-1", "tests_dim")[0].resolve()
    with store.lease(repo_id=1) as con, pytest.raises(duckdb.Error):
        con.execute(f"SELECT count(*) FROM read_parquet('{other}')")
    with store.lease(repo_id=2) as con:
        assert con.execute(f"SELECT count(*) FROM read_parquet('{other}')").fetchone()[0] > 0
    with pytest.raises(duckdb.Error):
        store.query(f"SELECT * FROM read_parquet('{other}')", repo_id=1, run_id="run-1")


@pytest.mark.security
def test_scopes_with_the_same_runs_never_share_a_connection(store: FactsStore) -> None:
    """Two run-less repos (and the unscoped and repo scope of a one-repo store) list the same runs
    (none); the cached connection must still be the scope's own, with its own allowed directories."""
    for repo in (5, 6):
        store.repo_dir(repo).mkdir(parents=True)
    secret = store.repo_dir(5) / "paths.parquet"
    pq.write_table(pa.table({"path": ["src/secret.py"]}), secret)
    probe = f"SELECT * FROM read_parquet('{secret.resolve()}')"
    assert store.query(probe, repo_id=5).num_rows == 1  # repo 5's own scope may read it
    with pytest.raises(duckdb.Error):
        store.query(probe, repo_id=6)
    assert store.query(probe).num_rows == 1  # the unscoped connection spans the root
    with pytest.raises(duckdb.Error):
        store.query(probe, repo_id=6)  # and its cached connection is not reused either


def test_connections_get_the_bounded_memory_limit_and_threads(
    facts: FactsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_FACTS_MEMORY_LIMIT", "300MB")
    monkeypatch.setenv("COVERSET_FACTS_THREADS", "2")
    con = facts.connect(repo_id=1, run_id="run-1", tables=("files",))
    try:
        limit, threads = con.execute(
            "SELECT current_setting('memory_limit'), current_setting('threads')"
        ).fetchone()
    finally:
        con.close()
    assert limit.startswith("286") and threads == 2  # 300 MB is 286.1 MiB
    monkeypatch.setenv("COVERSET_FACTS_MEMORY_LIMIT", "1GB'; SET threads=64; --")
    assert cache.memory_limit() == "512MB"  # never interpolated as SQL


# -- memoised results ------------------------------------------------------------------------------


def test_memoised_results_are_hits_until_the_run_changes(facts: FactsStore) -> None:
    first = queries.treemap(facts, 1, "run-2")
    assert queries.treemap(facts, 1, "run-2") == first
    assert cache.RESULTS.stats.hits == 1
    first.clear()  # a caller's change never reaches the cache
    assert queries.treemap(facts, 1, "run-2")
    finalize_run(
        facts, make_ctx("run-2", day=2, head_sha="c" * 40), expected_shards=1, catalogue=int_catalogue
    )
    after = {n.path: n for n in queries.treemap(facts, 1, "run-2")}
    assert cache.RESULTS.stats.invalidations >= 1
    assert any(n.input_cov is not None for n in after.values())  # func_input_cov is in now


def test_the_result_cache_is_bounded() -> None:
    lru = cache.ResultCache(max_entries=3)
    for key in range(5):
        assert lru.get_or_compute(key, "s", lambda key=key: [key]) == [key]
    assert len(lru) == 3 and lru.stats.evictions == 2
    assert lru.get_or_compute(4, "s", lambda: ["recomputed"]) == [4]
    assert lru.get_or_compute(4, "t", lambda: ["recomputed"]) == ["recomputed"]


# -- per-run summaries -------------------------------------------------------------------------------


def _uncached(facts: FactsStore) -> FactsStore:
    """The same files with every summary hidden (as for a run finalized before they existed)."""
    for run in facts.list_runs(1):
        for table in SUMMARY_TABLES:
            facts.delete_table(1, run, table)
    cache.RESULTS.clear()
    return facts


def test_finalize_writes_the_summaries_and_an_ingest_drops_them(
    facts: FactsStore, minimal_bundle: Path, functions: list[FunctionRegion]
) -> None:
    for table in SUMMARY_TABLES:
        assert facts.table_files(1, "run-1", table), table
        assert queries.summary_is_fresh(facts, 1, "run-1", table)
        assert not facts.table_files(1, "run-2", table)
    bundle = read_python_bundle(minimal_bundle)
    ingest_shard(
        facts, make_ctx("run-1", day=1), split_bundle(bundle, {T_NEG}, with_import=True), 1, functions
    )
    assert not any(facts.table_files(1, "run-1", table) for table in SUMMARY_TABLES)


def test_summaries_answer_exactly_what_test_lines_answers(facts: FactsStore) -> None:
    with_summaries = (
        queries.treemap(facts, 1, "run-1"),
        queries.top_gaps(facts, 1, "run-1", 10),
        queries.function_history(facts, 1, queries.treemap(facts, 1, "run-1")[-1].func_key or "", "main"),
    )
    line_hits = facts.read_table(1, "run-1", "line_hits").to_pylist()
    computed = _uncached(facts)
    assert not queries.summary_is_fresh(computed, 1, "run-1", "func_body")
    without = (
        queries.treemap(computed, 1, "run-1"),
        queries.top_gaps(computed, 1, "run-1", 10),
        queries.function_history(computed, 1, with_summaries[0][-1].func_key or "", "main"),
    )
    assert with_summaries == without
    rows = computed.read_table(1, "run-1", "test_lines").to_pylist()
    expected: dict[tuple[int, int], tuple[set[int], bool]] = {}
    for row in rows:
        tests, by_import = expected.setdefault((row["file_id"], row["line"]), (set(), False))
        if row["test_id"] == 0:
            expected[(row["file_id"], row["line"])] = (tests, True)
        else:
            tests.add(row["test_id"])
    assert {(r["file_id"], r["line"]): (r["tests"], r["by_import"]) for r in line_hits} == {
        key: (len(tests), by_import) for key, (tests, by_import) in expected.items()
    }


def test_class_changes_read_the_summaries_of_both_runs(facts: FactsStore) -> None:
    finalize_run(
        facts, make_ctx("run-2", day=2, head_sha="c" * 40), expected_shards=1, catalogue=int_catalogue
    )
    with_summaries = queries.class_changes(facts, 1, "run-2", "run-1")
    assert with_summaries  # run-2 ran fewer tests: some body lines are newly uncovered
    assert with_summaries == queries.class_changes(_uncached(facts), 1, "run-2", "run-1")


def test_a_summary_older_than_its_sources_is_ignored(facts: FactsStore) -> None:
    """Facts edited out of band (here: hits of one line dropped from test_lines after finalize) are
    read from test_lines, never from the stale summary."""
    before = {n.path: n for n in queries.treemap(facts, 1, "run-1")}
    leaf = next(n for n in before.values() if n.path.endswith("::classify"))
    for path in facts.table_files(1, "run-1", "test_lines"):
        table = pq.read_table(path)
        drop = pc.equal(table["line"], leaf.def_line + 2)
        pq.write_table(table.filter(pc.invert(drop)), path)
        future = path.stat().st_mtime_ns + 10**9
        os.utime(path, ns=(future, future))
    assert not queries.summary_is_fresh(facts, 1, "run-1", "tree_leaf")
    after = {n.path: n for n in queries.treemap(facts, 1, "run-1")}
    assert after[leaf.path].covered == leaf.covered - 1


# -- the web's indexed run facts ------------------------------------------------------------------------


def _view(view: object) -> dict[str, object]:
    """A function view's fields, except its catalogue (an object compared by identity)."""
    return {f.name: getattr(view, f.name) for f in dataclasses.fields(view) if f.name != "catalogue"}  # type: ignore[arg-type]


@pytest.mark.parametrize("run", ["run-1", "run-2"])
def test_indexed_run_facts_answer_what_run_facts_answers(facts: FactsStore, run: str) -> None:
    """``IndexedRunFacts`` (summaries and indexes) against the base class (``test_lines`` scans)."""
    fast, slow = IndexedRunFacts(facts, 1, run), RunFacts(facts, 1, run)
    assert (fast._hits is not None) == (run == "run-1")  # only the finalized run has line_hits
    for path in [*slow.statics, "src/missing.py"]:
        assert fast.line_facts(path) == slow.line_facts(path)
        assert fast.partial(path) == slow.partial(path)
        assert fast.callables(path) == slow.callables(path)
        for line in range(0, 40):
            assert fast.import_covers(path, line) == slow.import_covers(path, line)
            assert fast.tests_in_range(path, line, line + 3) == slow.tests_in_range(path, line, line + 3)
    assert fast.union_lines() == slow.union_lines()
    for region in slow.regions:
        assert fast.region(region.func_key) == slow.region(region.func_key)
        assert fast.sampling(region) == slow.sampling(region)
        assert fast.observations(region.func_key) == slow.observations(region.func_key)
        assert fast.body_coverage(region) == slow.body_coverage(region)
        source = "def add(a, b):\n    return a + b\n"
        assert _view(fast.function_view(region, source)) == _view(slow.function_view(region, source))
        assert fast.function_view(region, source) is fast.function_view(region, source)  # memoised
    assert fast.region("0" * 16) is None and fast.memory_bytes() > 0


def test_estimate_bytes_counts_buffers_and_each_object_once() -> None:
    import sys

    import numpy as np

    from coverset.web.loaders import estimate_bytes

    array = np.zeros(100_000, dtype=np.int32)
    assert 400_000 <= estimate_bytes(array) < 401_000
    table = pa.table({"x": pa.array(range(50_000), pa.int64())})
    assert estimate_bytes(table) == table.nbytes
    shared = "x" * 10_000
    assert estimate_bytes([shared, shared]) < 2 * sys.getsizeof(shared)  # counted once
    texts = [f"{i:08d}" * 8 for i in range(20_000)]  # sampled and extrapolated
    exact = sys.getsizeof(texts) + sum(sys.getsizeof(t) for t in texts)
    assert 0.9 * exact <= estimate_bytes(texts) <= 1.1 * exact
    nested = {i: (frozenset(range(i % 50)), [str(i)] * 3) for i in range(5_000)}
    assert estimate_bytes(nested) > sys.getsizeof(nested) + 5_000 * sys.getsizeof(frozenset())


def test_memory_bytes_weighs_every_loaded_view(facts: FactsStore) -> None:
    """Not only the per-line arrays: the ``call_shapes`` Arrow table and the Python views count."""
    fast = IndexedRunFacts(facts, 1, "run-1")
    empty = fast.memory_bytes()
    table, _spans, _triples = fast._shape_rows
    with_shapes = fast.memory_bytes()
    assert with_shapes - empty >= table.nbytes > 0
    _ = fast.statics, fast.regions, fast.input_cov_rows, fast._sampling_rows
    with_views = fast.memory_bytes()
    assert with_views > with_shapes
    assert fast.memory_bytes() == with_views  # a loaded view is weighed once
    for region in fast.callables():
        fast.function_view(region, None)
        fast.observations(region.func_key)
    assert fast.memory_bytes() > with_views  # the memo entries count too


def test_cached_run_facts_follow_the_run_stamp(facts: FactsStore) -> None:
    first = cached_run_facts(facts, 1, "run-2")
    assert cached_run_facts(facts, 1, "run-2") is first
    finalize_run(facts, make_ctx("run-2", day=2, head_sha="c" * 40), expected_shards=1)
    again = cached_run_facts(facts, 1, "run-2")
    assert again is not first and again._hits is not None


def test_cached_run_facts_stay_within_the_byte_budget(
    facts: FactsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coverset.web.loaders as loaders

    one = cached_run_facts(facts, 1, "run-1")
    one.line_facts(MODULE_PATH)
    _ = one._lines  # load the per-line arrays: the entry now weighs something
    monkeypatch.setattr(loaders, "run_facts_budget", lambda: 1)
    two = cached_run_facts(facts, 1, "run-2")
    assert cached_run_facts(facts, 1, "run-2") is two  # the newest entry always stays
    assert cached_run_facts(facts, 1, "run-1") is not one  # evicted for the budget, rebuilt


def test_the_budget_is_checked_on_hits_too(facts: FactsStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry weighs almost nothing when inserted (it loads lazily); what a page loaded into it
    afterwards is counted by the next lookup, a hit of another run included."""
    import coverset.web.loaders as loaders

    one = cached_run_facts(facts, 1, "run-1")
    two = cached_run_facts(facts, 1, "run-2")
    assert loaders.run_facts_weight()[0] == 2
    _ = one._lines, one._shape_rows, one.statics, one.regions  # a page fills run-1 after insertion
    two_weight = two.memory_bytes()
    monkeypatch.setattr(loaders, "run_facts_budget", lambda: two_weight + one.memory_bytes() - 1)
    assert cached_run_facts(facts, 1, "run-2") is two  # a hit: nothing inserted
    assert loaders.run_facts_weight()[0] == 1  # run-1, least recently used, was evicted
    assert cached_run_facts(facts, 1, "run-1") is not one


def _git_mirror(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    """A bare mirror with one commit of ``files``, and that commit's sha."""
    import subprocess

    work, mirror = tmp_path / "work", tmp_path / "mirror.git"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for path, text in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "c"]):
        subprocess.run(["git", "-C", str(work), *args], check=True, env=env)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(mirror)], check=True, env=env)
    sha = subprocess.run(
        ["git", "-C", str(mirror), "rev-parse", "HEAD"], check=True, env=env, capture_output=True, text=True
    ).stdout.strip()
    return mirror, sha


def test_a_failing_source_prefetch_falls_back_to_one_path(tmp_path: Path) -> None:
    """A measured path git cannot answer in a batch (missing, with a space: ``read_blobs`` cannot
    parse git's reply) must not fail the source page of every other file of the run."""
    from coverset.web import loaders

    mirror, sha = _git_mirror(tmp_path, {"README.md": "hello\n", "src/a.py": "x = 1\n"})
    measured = ["gen/my file.py", "README.md", "src/a.py"]
    assert loaders._read_text(mirror, sha, "README.md", measured) == "hello\n"
    assert loaders._read_text(mirror, sha, "src/a.py", measured) == "x = 1\n"
    assert loaders._read_text(mirror, sha, "README.md", measured) == "hello\n"  # cached


def test_a_prefetch_error_is_remembered_per_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coverset.web import loaders

    mirror, sha = _git_mirror(tmp_path, {"a.py": "a\n", "b.py": "b\n"})
    real = loaders.read_blobs
    batches: list[int] = []

    def flaky(mirror: Path, sha: str, paths: list[str]) -> dict[str, bytes]:
        batches.append(len(paths))
        if len(paths) > 1:
            raise ValueError("unparseable batch reply")
        return real(mirror, sha, paths)

    monkeypatch.setattr(loaders, "read_blobs", flaky)
    assert loaders._read_text(mirror, sha, "a.py", ["a.py", "b.py"]) == "a\n"
    assert loaders._read_text(mirror, sha, "b.py", ["a.py", "b.py"]) == "b\n"
    assert batches == [2, 1, 1]  # the batch failed once; later misses read one path each
