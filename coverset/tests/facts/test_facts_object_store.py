"""The facts store over a blob store, as several replicas see it.

``replicas`` hands out :class:`FactsStore` objects that share one blob store, each with its own cache
directory in front of it, which is what two control-plane replicas (or a process with an evicted
cache) are: for ``local`` the store is files in one directory (the default, ``LocalBlobStore``), for
``s3`` one S3 prefix (moto, or ``COVERSET_TEST_BLOB_URL``). Every test runs on both: one code path,
the same answers.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from facts_helpers import TESTS, calc_functions, int_catalogue, make_ctx, split_bundle

from coverset.facts import cache, flows, queries
from coverset.facts.convert import read_python_bundle
from coverset.facts.gc import expire_runs
from coverset.facts.ingest import finalize_run, ingest_shard
from coverset.facts.store import MANIFEST_FILE, FactsStore, RunWriteConflict
from coverset.lang.base import NeutralBundle
from coverset.storage import BlobStore, LocalBlobStore, S3BlobStore, blob_url_of, store_from_url
from coverset.storage.testing import fresh_blob_url, local_facts_store

Replicas = Callable[[str], FactsStore]


@pytest.fixture
def bundle(minimal_bundle: Path) -> NeutralBundle:
    return read_python_bundle(minimal_bundle)


@pytest.fixture(params=["local", "s3"])
def replicas(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Replicas]:
    """``replicas(name)``: a store per replica name (see the module docstring)."""
    cache.configure({cache.SYNC_INTERVAL_ENV: "0"})  # every read checks the manifest (tests below set it)
    blobs: BlobStore
    if request.param == "local":
        blobs = LocalBlobStore(tmp_path / "blobs")
    else:
        blobs = S3BlobStore.from_url(fresh_blob_url("replicas"))
    yield lambda name: FactsStore(tmp_path / name / "facts", blobs)
    blobs.delete([info.key for info in blobs.list()])


def _ingest(store: FactsStore, bundle: NeutralBundle, run_id: str = "run-1", **ctx: object) -> None:
    context = make_ctx(run_id, **ctx)  # type: ignore[arg-type]
    ingest_shard(store, context, bundle, 0, calc_functions())
    finalize_run(store, context, expected_shards=1, catalogue=int_catalogue)


def test_a_second_replica_answers_every_page_query_like_the_writer(
    replicas: Replicas, bundle: NeutralBundle, tmp_path: Path
) -> None:
    writer, reader = replicas("writer"), replicas("reader")
    _ingest(writer, bundle, "base", day=1)
    _ingest(writer, bundle, "head", day=2, pr_number=7)
    reference = local_facts_store(tmp_path / "reference")
    _ingest(reference, bundle, "base", day=1)
    _ingest(reference, bundle, "head", day=2, pr_number=7)
    path = "src/factspkg/calc.py"
    for store in (writer, reader):
        assert store.list_repos() == [1] and store.list_runs(1) == ["base", "head"]
        for run in ("base", "head"):
            assert queries.run_summary(store, 1, run) == queries.run_summary(reference, 1, run)
            assert queries.treemap(store, 1, run) == queries.treemap(reference, 1, run)
            assert queries.top_gaps(store, 1, run) == queries.top_gaps(reference, 1, run)
            assert queries.tests_covering(store, 1, run, path, 1, 40) == queries.tests_covering(
                reference, 1, run, path, 1, 40
            )
            assert queries.test_touched(store, 1, run, TESTS[0]) == queries.test_touched(
                reference, 1, run, TESTS[0]
            )
            assert queries.patch_coverage(store, 1, run, {path: {5, 10, 11}}) == queries.patch_coverage(
                reference, 1, run, {path: {5, 10, 11}}
            )
        assert queries.class_changes(store, 1, "head", "base") == queries.class_changes(
            reference, 1, "head", "base"
        )
        assert queries.baseline_trend(store, 1, "main") == queries.baseline_trend(reference, 1, "main")
        assert [p.run_id for p in queries.baseline_trend(store, 1, "main")] == ["base"], "head is a PR run"
        assert store.dictionary(1).nodeid(1) == reference.dictionary(1).nodeid(1)


def test_the_reader_keeps_the_summaries_fresh(replicas: Replicas, bundle: NeutralBundle) -> None:
    """A download gets its writer's mtime, so a summary still reads as newer than its sources."""
    writer, reader = replicas("writer"), replicas("reader")
    _ingest(writer, bundle)
    for name in ("func_body", "tree_leaf", "line_hits"):
        assert queries.summary_is_fresh(reader, 1, "run-1", name)
    manifest = reader.manifest(1, "run-1")
    assert manifest is not None
    for name, entry in manifest["files"].items():
        assert (reader.run_dir(1, "run-1") / name).stat().st_mtime_ns == entry["mtime_ns"]


def test_the_manifest_lists_the_run_and_its_version_only_grows(
    replicas: Replicas, bundle: NeutralBundle
) -> None:
    store = replicas("writer")
    context = make_ctx("run-1")
    ingest_shard(store, context, split_bundle(bundle, {TESTS[0]}, with_import=True), 0, calc_functions())
    first = store.manifest(1, "run-1")
    finalize_run(store, context, expected_shards=1)
    second = store.manifest(1, "run-1")
    assert first is not None and second is not None and second["version"] > first["version"]
    on_disk = sorted(p.name for p in store.run_dir(1, "run-1").glob("*.parquet"))
    assert sorted(second["files"]) == on_disk
    assert (store.run_dir(1, "run-1") / MANIFEST_FILE).is_file()
    stored = json.loads(store.blobs.get_bytes(f"facts/repo=1/run=run-1/{MANIFEST_FILE}")[0])
    assert stored == second
    keys = {i.key.rsplit("/", 1)[1] for i in store.blobs.list("facts/repo=1/run=run-1/")}
    assert keys == {*(e["key"] for e in second["files"].values()), MANIFEST_FILE}, (
        "superseded objects are deleted after the manifest"
    )
    assert keys.isdisjoint(on_disk), "an object is one version of a file, never the file's own name"
    assert first["files"]["runs_dim.parquet"]["key"] != second["files"]["runs_dim.parquet"]["key"]
    assert second["created_at"] == "2026-10-01T12:00:00+00:00", "retention reads the age from here"


def test_a_reader_trusts_its_last_check_for_the_sync_interval(
    replicas: Replicas, bundle: NeutralBundle
) -> None:
    writer, reader = replicas("writer"), replicas("reader")
    context = make_ctx("run-1")
    ingest_shard(writer, context, split_bundle(bundle, {TESTS[0]}, with_import=True), 0, calc_functions())
    assert reader.committed(1, "run-1") == {0: 1}
    cache.configure({cache.SYNC_INTERVAL_ENV: "3600"})
    ingest_shard(writer, context, split_bundle(bundle, {TESTS[1]}, with_import=False), 1, calc_functions())
    assert reader.committed(1, "run-1") == {0: 1}, "within the interval: the last check's answer"
    cache.configure({cache.SYNC_INTERVAL_ENV: "0"})
    assert reader.committed(1, "run-1") == {0: 1, 1: 1}
    assert reader.dictionary(1).find_test_id(TESTS[1]) is not None, "the dictionary follows the run"


def test_a_failed_publish_leaves_readers_on_the_previous_version(
    replicas: Replicas, bundle: NeutralBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, reader = replicas("writer"), replicas("reader")
    context = make_ctx("run-1")
    ingest_shard(writer, context, split_bundle(bundle, {TESTS[0]}, with_import=True), 0, calc_functions())
    before = queries.run_summary(reader, 1, "run-1")
    blobs = writer.blobs
    original = blobs.put_bytes

    def refuse_manifests(key: str, data: bytes, **kwargs: object) -> str:
        if key.endswith(MANIFEST_FILE):
            raise ConnectionError("network gone")
        return original(key, data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(blobs, "put_bytes", refuse_manifests)
    with pytest.raises(ConnectionError):
        ingest_shard(
            writer, context, split_bundle(bundle, {TESTS[1]}, with_import=False), 1, calc_functions()
        )
    assert reader.committed(1, "run-1") == {0: 1}, "the new shard's objects are there, unreferenced"
    assert queries.run_summary(reader, 1, "run-1") == before
    monkeypatch.setattr(blobs, "put_bytes", original)
    finalize_run(writer, context, expected_shards=2)  # the next publish carries the earlier write too
    assert reader.committed(1, "run-1") == {0: 1, 1: 1}


def test_a_lost_race_raises_instead_of_overwriting(replicas: Replicas, bundle: NeutralBundle) -> None:
    first, second = replicas("first"), replicas("second")
    context = make_ctx("run-1")
    ingest_shard(first, context, split_bundle(bundle, {TESTS[0]}, with_import=True), 0, calc_functions())
    with pytest.raises(RunWriteConflict), second.run_lock(1, "run-1"):
        # Another replica publishes while this one holds its (local) lock: two claimants of one run.
        ingest_shard(first, context, split_bundle(bundle, {TESTS[1]}, with_import=False), 1, calc_functions())
        second.write_table(1, "run-1", "runs_dim", first.read_table(1, "run-1", "runs_dim"))
    assert second.committed(1, "run-1") == {0: 1, 1: 1}, "the winner's version is what everyone reads"
    manifest = first.manifest(1, "run-1")
    assert manifest is not None
    keys = {i.key.rsplit("/", 1)[1] for i in first.blobs.list("facts/repo=1/run=run-1/")}
    assert keys == {*(e["key"] for e in manifest["files"].values()), MANIFEST_FILE}, (
        "the loser's uploads are deleted: nothing references them"
    )


def _die_on_manifest_put(blobs: BlobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer process dies right after its uploads, before the manifest (a deploy, an OOM kill)."""
    original = blobs.put_bytes

    def put_bytes(key: str, data: bytes, **kwargs: object) -> str:
        if key.endswith(MANIFEST_FILE):
            raise SystemExit(137)
        return original(key, data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(blobs, "put_bytes", put_bytes)


def test_a_writer_that_dies_after_its_uploads_leaves_a_readable_run(
    replicas: Replicas, bundle: NeutralBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run files are rewritten on every ingest (runs_dim, the ingest log, the aggregates), but as new
    objects: the manifest's version is complete whatever happened to the writer, so a fresh replica
    reads it, another host can take the run over, and gc still ages it."""
    writer = replicas("writer")
    context = make_ctx("run-1")
    ingest_shard(writer, context, split_bundle(bundle, {TESTS[0]}, with_import=True), 0, calc_functions())
    before = queries.run_summary(replicas("check"), 1, "run-1")
    _die_on_manifest_put(writer.blobs, monkeypatch)
    with pytest.raises(SystemExit):
        ingest_shard(
            writer, context, split_bundle(bundle, {TESTS[1]}, with_import=False), 1, calc_functions()
        )
    monkeypatch.undo()
    fresh = replicas("fresh")  # an empty cache: a new container, or one that evicted the run
    assert fresh.committed(1, "run-1") == {0: 1}
    assert queries.run_summary(fresh, 1, "run-1") == before
    assert expire_runs(
        replicas("gc"), keep_run_ids=set(), older_than=datetime(2026, 10, 2, tzinfo=UTC), dry_run=True
    ) == ["run-1"]
    other = replicas("other-host")
    finalize_run(other, context, expected_shards=1)  # another host takes the run over
    assert replicas("fresh-2").committed(1, "run-1") == {0: 1}
    assert queries.run_summary(replicas("fresh-2"), 1, "run-1") is not None
    # The dead writer's host comes back: taking the lock pulls the other host's version (its own
    # unpublished shard is discarded, the uploads it made are unreferenced), and it ingests on top.
    ingest_shard(writer, context, split_bundle(bundle, {TESTS[1]}, with_import=False), 1, calc_functions())
    assert replicas("fresh-3").committed(1, "run-1") == {0: 1, 1: 1}
    manifest = writer.manifest(1, "run-1")
    assert manifest is not None and manifest["version"] == 3, "v1 shard 0, v2 finalize, v3 shard 1"


def test_a_failed_pull_is_not_retried_before_the_sync_interval(
    replicas: Replicas, bundle: NeutralBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, reader = replicas("writer"), replicas("reader")
    _ingest(writer, bundle)
    blobs = reader.blobs
    calls = {"head": 0, "get": 0}
    original_head, original_get = blobs.head, blobs.get_bytes

    def head(key: str) -> object:
        calls["head"] += 1
        return original_head(key)

    def get_bytes(key: str) -> tuple[bytes, str]:
        calls["get"] += 1
        raise ConnectionError("the store is unreachable")

    monkeypatch.setattr(blobs, "head", head)
    monkeypatch.setattr(blobs, "get_bytes", get_bytes)
    cache.configure({cache.SYNC_INTERVAL_ENV: "3600"})
    with pytest.raises(ConnectionError):
        reader.committed(1, "run-1")
    with pytest.raises(ConnectionError):
        reader.committed(1, "run-1")  # the same error again, without asking the store
    assert calls == {"head": 1, "get": 1}, "one attempt per sync interval"
    with pytest.raises(ConnectionError):
        reader.has_run(1, "run-1")  # every path into the run goes through the same check
    assert calls == {"head": 1, "get": 1}
    monkeypatch.setattr(blobs, "get_bytes", original_get)
    cache.configure({cache.SYNC_INTERVAL_ENV: "0"})
    assert reader.committed(1, "run-1") == {0: 1}, "the next interval tries again and succeeds"
    # A reader that holds a copy keeps serving it while the store is down (the check that failed
    # included), and asks again after the interval.
    ingest_shard(
        writer, make_ctx("run-1"), split_bundle(bundle, {TESTS[1]}, with_import=False), 1, calc_functions()
    )
    monkeypatch.setattr(blobs, "get_bytes", get_bytes)
    gets = calls["get"]
    assert reader.committed(1, "run-1") == {0: 1}, "the check failed: the previous version, not an error"
    assert calls["get"] == gets + 1
    cache.configure({cache.SYNC_INTERVAL_ENV: "3600"})
    assert reader.committed(1, "run-1") == {0: 1} and calls["get"] == gets + 1, "not asked again"
    cache.configure({cache.SYNC_INTERVAL_ENV: "0"})
    monkeypatch.setattr(blobs, "get_bytes", original_get)
    assert reader.committed(1, "run-1") == {0: 1, 1: 1}


def test_a_manifest_naming_a_missing_object_is_an_error_not_a_loop(
    replicas: Replicas, bundle: NeutralBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = replicas("writer")
    _ingest(writer, bundle)
    manifest = writer.manifest(1, "run-1")
    assert manifest is not None
    gone = manifest["files"]["runs_dim.parquet"]["key"]
    writer.blobs.delete([f"facts/repo=1/run=run-1/{gone}"])  # behind coverset's back
    monkeypatch.setattr("coverset.facts.store.time.sleep", lambda _s: None)
    with pytest.raises(RunWriteConflict, match="missing"):
        replicas("fresh").committed(1, "run-1")


def test_two_writers_never_hand_out_the_same_id(replicas: Replicas) -> None:
    """Two replicas extend one repo's dictionary at once: every id each was given stays its value."""
    stores = [replicas("a"), replicas("b")]
    given: list[dict[str, int]] = [{}, {}]
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        try:
            for round_ in range(12):
                values = [f"tests/w{index}.py::t{round_}_{n}" for n in range(3)]
                ids = stores[index].extend_dictionary(1, nodeids=values)
                for value in values:
                    given[index][value] = ids.test_id(value)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    final = replicas("check").dictionary(1)
    for mapping in given:
        for value, test_id in mapping.items():
            assert final.nodeid(test_id) == value
    assert len({*given[0].values(), *given[1].values()}) == 72


def test_delete_and_gc_reach_every_replica(replicas: Replicas, bundle: NeutralBundle) -> None:
    writer, reader, janitor = replicas("writer"), replicas("reader"), replicas("janitor")
    _ingest(writer, bundle, "old", day=1)
    _ingest(writer, bundle, "new", day=5)
    assert reader.list_runs(1) == ["new", "old"]
    assert expire_runs(janitor, keep_run_ids=set(), older_than=datetime(2026, 10, 3, tzinfo=UTC)) == ["old"]
    assert reader.list_runs(1) == ["new"] and not reader.has_run(1, "old")
    assert not reader.run_dir(1, "old").exists() or not list(reader.run_dir(1, "old").glob("*.parquet"))
    assert writer.delete_run(1, "new")
    assert reader.list_runs(1) == [] and queries.run_summary(reader, 1, "new") is None
    assert [i.key for i in writer.blobs.list("facts/repo=1/")] == ["facts/repo=1/dictionary.json"]
    assert reader.dictionary(1).nodeid(1), "gc never touches the dictionary"


def test_production_batches_travel_between_replicas(replicas: Replicas, bundle: NeutralBundle) -> None:
    from test_flows import lines, payload

    writer, reader = replicas("writer"), replicas("reader")
    _ingest(writer, bundle)
    result = flows.import_payloads(writer, 1, lines(payload()))
    assert result.windows == 1
    windows = flows.list_windows(reader, 1)
    assert [(w.service, w.env) for w in windows] == [("calc-api", "production")]
    assert flows.read_prod(reader, 1).shapes == flows.read_prod(writer, 1).shapes
    assert reader.list_runs(1) == ["run-1"], "production data is never a run"
    assert len(flows.prune_windows(reader, 1, older_than=datetime(2026, 10, 14, tzinfo=UTC))) == 1
    assert flows.list_windows(writer, 1) == []


def test_a_worker_builds_the_apps_store_from_the_facts_dir_and_the_blob_url(
    tmp_path: Path, bundle: NeutralBundle
) -> None:
    """What an ingest worker process does: its job carries the facts directory and the blob URL
    (``IngestJob``), and the store it builds publishes where the app's own store reads."""
    from coverset.config import Settings
    from coverset.facts.store import facts_store

    cache.configure({cache.SYNC_INTERVAL_ENV: "0"})
    settings = Settings.for_tests(data_dir=tmp_path / "data")
    worker = FactsStore(settings.facts_dir, store_from_url(blob_url_of(settings)))
    _ingest(worker, bundle)
    app_store = facts_store(settings)
    assert app_store.blobs is worker.blobs, "one store object per URL and process"
    assert app_store.committed(1, "run-1") == {0: 1}
    assert (settings.data_dir / "facts" / "repo=1" / "run=run-1" / MANIFEST_FILE).is_file()


def test_going_to_the_run_directory_pulls_it(replicas: Replicas, bundle: NeutralBundle) -> None:
    """Code that stamps or lists a run's directory itself (the web's run-facts cache, the PR view's
    diff cache, ``is_dir`` checks) finds the published run on a replica that never saw it."""
    writer, reader = replicas("writer"), replicas("reader")
    _ingest(writer, bundle)
    assert len(cache.run_stamp(reader.run_dir(1, "run-1"))) == len(
        cache.run_stamp(writer.run_dir(1, "run-1"))
    )
    assert reader.run_dir(1, "run-1").is_dir() and not reader.run_dir(1, "never").exists()
    assert (reader.repo_dir(1) / "dictionary.json").is_file()
