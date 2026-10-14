"""Upload archives in the store (archive, restore, retention) and the bounded local cache
(least-recently-used eviction that never removes what is in use), plus ``coverset cache``."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest
from click.testing import CliRunner

from coverset.config import Settings
from coverset.facts import cache
from coverset.facts.store import FactsStore
from coverset.facts.untrusted import BundleLimits
from coverset.storage import BlobStore, cachedir, store_from_url
from coverset.storage.testing import fresh_blob_url
from coverset.storage.uploads import archive_upload, remove_uploads, restore_upload, upload_key

SHA = "e" * 40


def _shard(root: Path, sha256: str) -> Path:
    (root / "sources" / "pkg").mkdir(parents=True)
    (root / "bundle.tar.gz").write_bytes(b"\x1f\x8b not really")
    (root / "sources" / "pkg" / "a.py").write_text("print(1)\n")
    (root / "upload.json").write_text(json.dumps({"label": "py313", "sha256": sha256}))
    return root


def test_upload_keys_are_checked() -> None:
    assert upload_key(4, SHA, "py3.13-linux") == f"uploads/4/{SHA}/py3.13-linux.tar.gz"
    for sha, label in (("HEAD", "x"), (SHA, "../x"), (SHA, "a/b"), (SHA, ".hidden")):
        with pytest.raises(ValueError):
            upload_key(4, sha, label)


def test_archive_restore_and_remove(blobs: BlobStore, tmp_path: Path) -> None:
    digest = "ab" * 32
    archive_upload(blobs, 4, SHA, "py313", _shard(tmp_path / "shard", digest))
    target = tmp_path / "runs" / "r1" / "upload" / "shard-0"
    limits = BundleLimits()
    assert not restore_upload(blobs, 4, SHA, "py313", target, limits, sha256_prefix="cd" * 16)
    assert not target.exists(), "an archive of another upload of the label is not this shard"
    assert restore_upload(blobs, 4, SHA, "py313", target, limits, sha256_prefix=digest[:32])
    assert (target / "sources" / "pkg" / "a.py").read_text() == "print(1)\n"
    assert oct(target.stat().st_mode & 0o777) == "0o700"
    assert not restore_upload(blobs, 4, SHA, "other", tmp_path / "x", limits)
    assert remove_uploads(blobs, 4, [SHA, "bad"], dry_run=True) == [SHA]
    assert remove_uploads(blobs, 4, [SHA]) == [SHA]
    assert not restore_upload(blobs, 4, SHA, "py313", tmp_path / "y", limits)


# -- the cache ------------------------------------------------------------------------------------


@pytest.fixture
def hosted(tmp_path: Path) -> Iterator[Settings]:
    settings = Settings.for_tests(data_dir=tmp_path / "data", blob_url=fresh_blob_url("cache"))
    cache.configure(settings.cache_overrides())
    yield settings
    blobs = store_from_url(settings.blob_url)
    blobs.delete([info.key for info in blobs.list()])


def _run(store: FactsStore, run_id: str, kb: int, used: float) -> Path:
    text = secrets.token_urlsafe(kb * 768)  # random: the Parquet file really is about kb KB
    store.write_table(1, run_id, "file_cov", pa.table({"file_id": [0], "path": [text]}))
    record = store.run_dir(1, run_id) / ".sync.json"
    os.utime(record, (used, used))
    return store.run_dir(1, run_id)


def test_settings_put_facts_and_mirrors_in_the_cache_with_an_object_store(tmp_path: Path) -> None:
    local = Settings.for_tests(data_dir=tmp_path / "data")
    assert local.facts_dir == local.data_dir / "facts" and local.mirrors_dir == local.data_dir / "mirrors"
    assert "COVERSET_BLOB_URL" not in local.cache_overrides()
    hosted = Settings.for_tests(data_dir=tmp_path / "data", blob_url="s3://b", cache_dir=tmp_path / "c")
    assert hosted.facts_dir == tmp_path / "c" / "facts" and hosted.mirrors_dir == tmp_path / "c" / "mirrors"
    assert hosted.cache_overrides() == {
        "COVERSET_BLOB_URL": "s3://b",
        "COVERSET_CACHE_DIR": str(tmp_path / "c"),
    }


def test_eviction_is_least_recently_used_and_spares_what_is_in_use(hosted: Settings) -> None:
    from coverset.facts.store import facts_store

    store = facts_store(hosted)
    now = time.time()
    oldest = _run(store, "oldest", 300, now - 300)
    locked = _run(store, "locked", 300, now - 250)
    middle = _run(store, "middle", 300, now - 200)
    newest = _run(store, "newest", 300, now - 100)
    unpublished = store.run_dir(1, "draft")
    unpublished.mkdir(parents=True)
    (unpublished / "files.shard=0.attempt=1.parquet").write_bytes(b"x" * 300_000)  # never published
    os.utime(unpublished, (now - 1000, now - 1000))
    mirror = hosted.mirrors_dir / "1.git"
    mirror.mkdir(parents=True)
    (mirror / "HEAD").write_text("ref: refs/heads/main\n")
    (mirror / "pack").write_bytes(b"p" * 300_000)
    os.utime(mirror / "HEAD", (now - 400, now - 400))
    os.utime(mirror, (now - 400, now - 400))
    status = cachedir.status(hosted)
    assert status.remote and status.by_kind["run"][0] == 5 and status.by_kind["mirror"][0] == 1
    sizes = {entry.path: entry.bytes for entry in cachedir.entries(hosted)}
    bound = status.total_bytes - sizes[mirror] - sizes[oldest] - 1  # those two, then one more
    with open(locked / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)  # an ingest holds this run
        report = cachedir.evict(hosted, max_bytes=bound)
    removed = {entry.path for entry in report.removed}
    assert removed == {mirror, oldest, middle}, "oldest first; locked and unpublished runs stay"
    assert locked.is_dir() and unpublished.is_dir() and newest.is_dir() and not oldest.exists()
    assert store.has_run(1, "oldest"), "an evicted run comes back from the bucket on its next read"
    assert store.read_table(1, "oldest", "file_cov").num_rows == 1


def test_the_local_default_has_no_cache_to_evict(tmp_path: Path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "data")
    FactsStore(settings.facts_dir).write_table(
        1, "r", "files", pa.table({"file_id": [0], "path": ["a"]}), shard_id=0, attempt=1
    )
    assert cachedir.evict(settings, max_bytes=0).removed == []
    assert cachedir.clear(settings).removed == [] and FactsStore(settings.facts_dir).list_runs(1) == ["r"]
    assert not cachedir.status(settings).remote


def test_the_cache_bound_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cache.CACHE_MAX_GB_ENV, raising=False)
    assert cache.cache_max_bytes() == 10_000_000_000
    for raw, expected in (("0.5", 500_000_000), ("-3", 0), ("lots", 10_000_000_000), ("nan", 10_000_000_000)):
        cache.configure({cache.CACHE_MAX_GB_ENV: raw})
        assert cache.cache_max_bytes() == expected
    cache.configure({cache.SYNC_INTERVAL_ENV: "2.5"})
    assert cache.sync_interval() == 2.5
    cache.configure({cache.SYNC_INTERVAL_ENV: "-1"})
    assert cache.sync_interval() == 5.0


def test_cache_command(hosted: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coverset.cli import cli
    from coverset.config import get_settings
    from coverset.facts.store import facts_store

    _run(facts_store(hosted), "r1", 10, time.time())
    env = {
        "COVERSET_DATA_DIR": str(hosted.data_dir),
        "COVERSET_BLOB_URL": hosted.blob_url,
        "COVERSET_CACHE_DIR": str(hosted.cache_dir),
    }
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    get_settings.cache_clear()  # the CLI reads its settings once per process
    status = runner.invoke(cli, ["cache", "status", "--json"], env=env)
    assert status.exit_code == 0, status.output
    parsed = json.loads(status.output)
    assert parsed["remote"] and parsed["by_kind"]["run"]["entries"] == 1
    cleared = runner.invoke(cli, ["cache", "clear", "--yes"], env=env)
    assert cleared.exit_code == 0 and "removed 1 entries" in cleared.output
    get_settings.cache_clear()
    local = runner.invoke(cli, ["cache", "clear", "--yes"], env={"COVERSET_DATA_DIR": str(tmp_path / "d")})
    assert local.exit_code == 0 and "nothing to clear" in local.output
    get_settings.cache_clear()
