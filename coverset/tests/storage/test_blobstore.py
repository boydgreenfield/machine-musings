"""The :class:`BlobStore` contract on both backends: round trips, conditional writes, multipart,
listing, deletion, key rules; plus URL parsing, the settings factory and the deploy-check probe."""

from __future__ import annotations

import os
import pickle
import stat
import threading
from pathlib import Path

import pytest

from coverset import storage
from coverset.config import Settings
from coverset.storage import (
    MULTIPART_THRESHOLD,
    BlobNotFound,
    BlobStore,
    BlobTooLarge,
    LocalBlobStore,
    PreconditionFailed,
    S3BlobStore,
    check_key,
    check_prefix,
    check_store,
    parse_blob_url,
    store_from_url,
)
from coverset.storage.testing import fresh_blob_url


def test_bytes_and_files_round_trip(blobs: BlobStore, tmp_path: Path) -> None:
    etag = blobs.put_bytes("a/b/one.bin", b"hello")
    data, got = blobs.get_bytes("a/b/one.bin")
    assert (data, got) == (b"hello", etag)
    source = tmp_path / "source.txt"
    source.write_bytes(b"x" * 1000)
    file_etag = blobs.put_file("a/two.txt", source)
    assert blobs.get_file("a/two.txt", tmp_path / "out" / "two.txt") == file_etag
    assert (tmp_path / "out" / "two.txt").read_bytes() == b"x" * 1000
    info = blobs.head("a/two.txt")
    assert info is not None and info.size == 1000 and info.etag == file_etag and info.key == "a/two.txt"
    assert blobs.head("a/missing") is None


def test_get_file_refuses_an_object_above_max_bytes_before_writing(blobs: BlobStore, tmp_path: Path) -> None:
    """A caller's bound on untrusted content, checked from the response's size: no HEAD first."""
    blobs.put_bytes("big", b"x" * 1000)
    with pytest.raises(BlobTooLarge):
        blobs.get_file("big", tmp_path / "big", max_bytes=999)
    assert not (tmp_path / "big").exists() and not list(tmp_path.glob(".*.tmp"))
    assert blobs.get_file("big", tmp_path / "big", max_bytes=1000)
    assert (tmp_path / "big").read_bytes() == b"x" * 1000


def test_a_missing_key_is_blob_not_found_and_file_not_found(blobs: BlobStore, tmp_path: Path) -> None:
    with pytest.raises(BlobNotFound):
        blobs.get_bytes("nope/x")
    with pytest.raises(FileNotFoundError):
        blobs.get_file("nope/x", tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_an_overwrite_changes_the_etag(blobs: BlobStore) -> None:
    first = blobs.put_bytes("k", b"one")
    second = blobs.put_bytes("k", b"two!")
    assert first != second and blobs.get_bytes("k") == (b"two!", second)


def test_if_match_and_if_none_match(blobs: BlobStore, tmp_path: Path) -> None:
    created = blobs.put_bytes("cas/dict.json", b"v1", if_none_match=True)
    with pytest.raises(PreconditionFailed):
        blobs.put_bytes("cas/dict.json", b"again", if_none_match=True)
    updated = blobs.put_bytes("cas/dict.json", b"v2", if_match=created)
    with pytest.raises(PreconditionFailed):
        blobs.put_bytes("cas/dict.json", b"stale", if_match=created)
    assert blobs.get_bytes("cas/dict.json") == (b"v2", updated)
    with pytest.raises(PreconditionFailed):  # the expected version does not exist at all
        blobs.put_bytes("cas/never.json", b"x", if_match=created)
    path = tmp_path / "f"
    path.write_bytes(b"v3")
    assert blobs.put_file("cas/dict.json", path, if_match=updated) != updated
    with pytest.raises(PreconditionFailed):
        blobs.put_file("cas/dict.json", path, if_match=updated)


def test_concurrent_compare_and_swap_loses_no_update(blobs: BlobStore) -> None:
    """Four threads each add 10 to a counter with an If-Match loop: every increment survives."""
    blobs.put_bytes("counter", b"0")

    def worker() -> None:
        for _ in range(10):
            while True:
                data, etag = blobs.get_bytes("counter")
                try:
                    blobs.put_bytes("counter", str(int(data) + 1).encode(), if_match=etag)
                    break
                except PreconditionFailed:
                    continue

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert blobs.get_bytes("counter")[0] == b"40"


def test_large_files_go_up_in_parts(blobs: BlobStore, tmp_path: Path) -> None:
    big = tmp_path / "big.parquet"
    payload = os.urandom(MULTIPART_THRESHOLD + 1024 * 1024)
    big.write_bytes(payload)
    etag = blobs.put_file("facts/repo=1/run=r/big.parquet", big)
    if isinstance(blobs, S3BlobStore):
        assert etag.endswith("-2"), "two 8 MiB parts: a multipart etag"
    out = tmp_path / "back.parquet"
    assert blobs.get_file("facts/repo=1/run=r/big.parquet", out) == etag
    assert out.read_bytes() == payload
    conditional = blobs.put_file("facts/repo=1/run=r/big.parquet", big, if_match=etag)
    assert blobs.head("facts/repo=1/run=r/big.parquet").etag == conditional  # type: ignore[union-attr]


def test_listing_stays_inside_its_prefix(blobs: BlobStore) -> None:
    for key in (
        "facts/repo=1/dictionary.json",
        "facts/repo=1/run=a/x.parquet",
        "facts/repo=12/run=b/y.parquet",
    ):
        blobs.put_bytes(key, b"1")
    assert sorted(i.key for i in blobs.list("facts/repo=1/")) == [
        "facts/repo=1/dictionary.json",
        "facts/repo=1/run=a/x.parquet",
    ]
    assert blobs.list_dirs("facts/") == ["repo=1", "repo=12"]
    assert blobs.list_dirs("facts/repo=1/") == ["run=a"]
    assert blobs.list_dirs("facts/repo=9/") == []
    with pytest.raises(ValueError):
        list(blobs.list("facts/repo=1"))  # a prefix ends at a segment boundary


def test_delete_and_delete_prefix(blobs: BlobStore) -> None:
    for key in ("u/1/a.tar.gz", "u/1/b.tar.gz", "u/2/c.tar.gz"):
        blobs.put_bytes(key, b"z")
    blobs.delete(["u/1/a.tar.gz", "u/1/missing"])
    assert blobs.head("u/1/a.tar.gz") is None and blobs.head("u/1/b.tar.gz") is not None
    assert blobs.delete_prefix("u/1/") == 1
    assert blobs.delete_prefix("u/1/") == 0
    assert [i.key for i in blobs.list("u/")] == ["u/2/c.tar.gz"]
    with pytest.raises(ValueError):
        blobs.delete_prefix("")


def test_copy_within_the_store(blobs: BlobStore) -> None:
    blobs.put_bytes("src/one", b"payload")
    etag = blobs.copy("src/one", "dst/one")
    assert blobs.get_bytes("dst/one") == (b"payload", etag)


@pytest.mark.parametrize(
    "key", ["sources/1/" + "a" * 40 + "/docs/naïve file.py", "sources/1/x/ünï cödé/ä b.txt"]
)
def test_unicode_and_spaces_round_trip_unchanged(blobs: BlobStore, key: str) -> None:
    blobs.put_bytes(key, "ok ✓".encode())
    assert blobs.get_bytes(key)[0] == "ok ✓".encode()
    assert [i.key for i in blobs.list(key.rsplit("/", 1)[0] + "/")] == [key]


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/abs",
        "a/../b",
        "./a",
        "a//b",
        "a\\b",
        "a\0b",
        "a/\nb",
        "a/\x7f",
        "a/.x.0123456789abcdef0123456789abcdef.tmp",
        ".blobstore.lock",
        "k" * 1025,
        "é" * 513,
    ],
)
def test_unsafe_keys_are_refused(blobs: BlobStore, key: str) -> None:
    with pytest.raises(ValueError):
        check_key(key)
    with pytest.raises(ValueError):
        blobs.put_bytes(key, b"x")
    with pytest.raises(ValueError):
        blobs.head(key)


def test_check_prefix() -> None:
    assert check_prefix("") == "" and check_prefix("facts/repo=1/") == "facts/repo=1/"
    for bad in ("facts", "../", "/x/"):
        with pytest.raises(ValueError):
            check_prefix(bad)


# -- the local backend ----------------------------------------------------------------------------


def test_local_files_are_private_and_written_atomically(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "root")
    store.put_bytes("sources/1/sha/pkg/mod.py", b"print(1)")
    path = store.local_path("sources/1/sha/pkg/mod.py")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["mod.py"], "no temporary file left behind"


def test_local_store_never_follows_a_symlink(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "evil").symlink_to(outside)
    with pytest.raises(ValueError):
        store.put_bytes("evil/x", b"redirected?")
    assert not (outside / "x").exists()
    (outside / "secret").write_bytes(b"s")
    (tmp_path / "root" / "link").symlink_to(outside / "secret")
    assert store.head("link") is None
    with pytest.raises(BlobNotFound):
        store.get_bytes("link")
    assert store.list_dirs("") == []
    assert [i.key for i in store.list("")] == []


def test_local_put_file_shares_the_bytes_and_get_file_copies(tmp_path: Path) -> None:
    """On one filesystem a stored file is a hard link (the facts cache and the store hold one copy):
    private like every object, with the etag of that inode. A download is always a copy, so its
    reader may set its mtime without touching the object."""
    store = LocalBlobStore(tmp_path / "root")
    source = tmp_path / "cache" / "runs_dim.parquet"
    source.parent.mkdir()
    source.write_bytes(b"facts")
    etag = store.put_file("facts/repo=1/run=r/runs_dim.v1.0a1b2c3d.parquet", source)
    stored = store.local_path("facts/repo=1/run=r/runs_dim.v1.0a1b2c3d.parquet")
    assert stored.stat().st_ino == source.stat().st_ino and source.stat().st_nlink == 2
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert store.head("facts/repo=1/run=r/runs_dim.v1.0a1b2c3d.parquet").etag == etag  # type: ignore[union-attr]
    copy = tmp_path / "elsewhere" / "runs_dim.parquet"
    assert store.get_file("facts/repo=1/run=r/runs_dim.v1.0a1b2c3d.parquet", copy) == etag
    assert copy.read_bytes() == b"facts" and copy.stat().st_ino != stored.stat().st_ino
    os.utime(copy, ns=(1, 1))
    assert store.head("facts/repo=1/run=r/runs_dim.v1.0a1b2c3d.parquet").etag == etag  # type: ignore[union-attr]


def test_local_put_file_copies_across_filesystems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    def cross_device(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", cross_device)
    store = LocalBlobStore(tmp_path / "root")
    source = tmp_path / "file"
    source.write_bytes(b"bytes")
    store.put_file("a/b", source, if_none_match=True)
    stored = store.local_path("a/b")
    assert stored.read_bytes() == b"bytes" and stored.stat().st_ino != source.stat().st_ino
    assert source.stat().st_nlink == 1 and stat.S_IMODE(stored.stat().st_mode) == 0o600


def test_local_delete_prunes_empty_directories(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "root")
    store.put_bytes("a/b/c/d", b"1")
    store.delete(["a/b/c/d"])
    assert not (tmp_path / "root" / "a").exists() and (tmp_path / "root").is_dir()


# -- URLs, the factory and the probe --------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("s3://my-bucket/coverset?region=eu-west-1", ("my-bucket", "coverset", None, "eu-west-1")),
        (
            "s3://coverset?endpoint_url=http://127.0.0.1:9000&region=us-east-1",
            ("coverset", "", "http://127.0.0.1:9000", "us-east-1"),
        ),
        (
            "s3://coverset/prod?endpoint_url=https://abc123.r2.cloudflarestorage.com&region=auto",
            ("coverset", "prod", "https://abc123.r2.cloudflarestorage.com", "auto"),
        ),
        (
            "s3://coverset?endpoint_url=https://fly.storage.tigris.dev&region=auto",
            ("coverset", "", "https://fly.storage.tigris.dev", "auto"),
        ),
    ],
)
def test_blob_url_forms(url: str, expected: tuple[str, str, str | None, str | None]) -> None:
    parsed = parse_blob_url(url)
    assert (parsed["bucket"], parsed["prefix"], parsed["endpoint_url"], parsed["region"]) == expected


@pytest.mark.parametrize(
    "url",
    [
        "gs://bucket",
        "s3://Bad_Bucket",
        "s3://key:secret@bucket",
        "s3://bucket?endpoint=http://x",
        "s3://bucket?endpoint_url=ftp://x",
        "s3://bucket/../x",
    ],
)
def test_bad_blob_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        parse_blob_url(url)


def test_one_store_object_per_url_and_it_pickles() -> None:
    url = "s3://pickle-test/prefix?endpoint_url=http://127.0.0.1:1&region=us-east-1"
    first = store_from_url(url)
    assert store_from_url(url) is first
    assert isinstance(first, S3BlobStore) and first.prefix == "prefix"
    clone = pickle.loads(pickle.dumps(first))
    assert clone.url == first.url and clone._client is None


def test_settings_pick_the_backend(tmp_path: Path) -> None:
    local = Settings.for_tests(data_dir=tmp_path / "data")
    found = storage.from_settings(local)
    assert isinstance(found, LocalBlobStore) and found.root == local.data_dir
    assert storage.blob_url_of(local) == local.data_dir.as_uri()
    assert storage.from_settings(local) is found, "one store object per URL"
    url = fresh_blob_url()
    hosted = Settings.for_tests(data_dir=tmp_path / "data", blob_url=url)
    assert storage.from_settings(hosted) is storage.store_from_url(url)


def test_a_data_dir_with_spaces_round_trips_through_its_url(tmp_path: Path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "my data ü")
    store = storage.from_settings(settings)
    assert isinstance(store, LocalBlobStore) and store.root == settings.data_dir


def test_check_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = Settings.for_tests(data_dir=tmp_path / "data")
    status, detail = check_store(local)
    assert status == "pass" and "conditional writes ok" in detail, "the same probe as a bucket's"
    assert not list(LocalBlobStore(local.data_dir).list("_coverset/")), "the probe cleans up"
    url = fresh_blob_url("probe")
    status, detail = check_store(Settings.for_tests(data_dir=tmp_path / "data", blob_url=url))
    assert status == "pass", detail
    assert not list(store_from_url(url).list("_coverset/")), "the probe cleans up"

    class Ignoring(LocalBlobStore):  # a store that drops If-Match on the floor
        def put_bytes(
            self, key: str, data: bytes, *, if_match: str | None = None, if_none_match: bool = False
        ) -> str:
            return super().put_bytes(key, data, if_none_match=if_none_match)

    monkeypatch.setitem(storage._STORES, "s3://ignoring", Ignoring(tmp_path / "ignoring"))
    status, detail = check_store(Settings.for_tests(data_dir=tmp_path / "data", blob_url="s3://ignoring"))
    assert status == "warn" and "If-Match" in detail
    unreachable = "s3://nobody?endpoint_url=http://127.0.0.1:9&region=us-east-1"
    status, detail = check_store(Settings.for_tests(data_dir=tmp_path / "data", blob_url=unreachable))
    assert status == "fail" and "s3://nobody" in detail
