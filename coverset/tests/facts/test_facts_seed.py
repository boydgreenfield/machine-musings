"""``coverset cache seed --from DIR``: a laptop's data directory published to the object store."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from facts_helpers import calc_functions, int_catalogue, make_ctx
from test_flows import lines, payload

from coverset.config import Settings
from coverset.facts import cache, flows, queries
from coverset.facts.convert import read_python_bundle
from coverset.facts.ingest import finalize_run, ingest_shard
from coverset.facts.sources import SourceStore
from coverset.facts.store import FactsStore, facts_store
from coverset.storage import store_from_url
from coverset.storage.migrate import seed_from_local
from coverset.storage.testing import fresh_blob_url

SHA = "a" * 40


@pytest.fixture
def hosted(tmp_path: Path) -> Iterator[Settings]:
    settings = Settings.for_tests(data_dir=tmp_path / "hosted", blob_url=fresh_blob_url("seed"))
    cache.configure({**settings.cache_overrides(), cache.SYNC_INTERVAL_ENV: "0"})
    yield settings
    blobs = store_from_url(settings.blob_url)
    blobs.delete([info.key for info in blobs.list()])


def test_a_local_data_directory_becomes_the_bucket(
    hosted: Settings, tmp_path: Path, minimal_bundle: Path
) -> None:
    laptop = tmp_path / "laptop"
    local = FactsStore(laptop / "facts")
    context = make_ctx("run-1")
    ingest_shard(local, context, read_python_bundle(minimal_bundle), 0, calc_functions())
    finalize_run(local, context, expected_shards=1, catalogue=int_catalogue)
    flows.import_payloads(local, 1, lines(payload()))
    uploaded = tmp_path / "up" / "src"
    uploaded.mkdir(parents=True)
    (uploaded / "a.py").write_text("print('a')\n")
    SourceStore(laptop).store(1, SHA, tmp_path / "up", allowed={"src/a.py"})
    shard = laptop / "runs" / "run-1" / "upload" / "shard-0"
    shard.mkdir(parents=True)
    (shard / "upload.json").write_text(json.dumps({"label": "py313", "head_sha": SHA, "sha256": "f" * 64}))

    report = seed_from_local(laptop, hosted)
    counts = (report.runs, report.dictionaries, report.batches, report.sources, report.uploads)
    assert counts == (1, 1, 1, 1, 1)
    assert not report.problems

    replica = FactsStore(tmp_path / "replica" / "facts", blobs=store_from_url(hosted.blob_url))
    assert replica.list_runs(1) == ["run-1"]
    assert queries.run_summary(replica, 1, "run-1") == queries.run_summary(local, 1, "run-1")
    assert queries.summary_is_fresh(replica, 1, "run-1", "func_body"), "mtimes travel with the files"
    assert [w.service for w in flows.list_windows(replica, 1)] == ["calc-api"]
    remote_sources = SourceStore(
        tmp_path / "r", blobs=store_from_url(hosted.blob_url), cache_dir=tmp_path / "rc"
    )
    assert remote_sources.read(1, SHA, "src/a.py") == b"print('a')\n"
    assert store_from_url(hosted.blob_url).head(f"uploads/1/{SHA}/py313.tar.gz") is not None

    again = seed_from_local(laptop, hosted)
    assert (again.runs, again.runs_skipped) == (0, 1)
    with pytest.raises(ValueError):
        seed_from_local(hosted.data_dir / "cache", hosted)  # the cache itself is no source
    assert facts_store(hosted).list_runs(1) == ["run-1"]
