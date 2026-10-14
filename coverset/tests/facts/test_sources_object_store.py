"""The uploaded-source store on both backends: the same rules, the first upload wins across
replicas, reads go through the cache, retention and purge remove objects and cached copies."""

from __future__ import annotations

import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from coverset.facts import cache
from coverset.facts.sources import MAX_SOURCE_FILE_BYTES, SourceStore, commit_dir
from coverset.storage import S3BlobStore
from coverset.storage.testing import fresh_blob_url

SHA = "c" * 40
OTHER = "d" * 40
Replicas = Callable[[str], SourceStore]


@pytest.fixture(params=["local", "s3"])
def replicas(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Replicas]:
    """``replicas(name)``: one data directory (local), or one prefix with a cache per replica (s3)."""
    if request.param == "local":
        yield lambda name: SourceStore(tmp_path / "data")
        return
    blobs = S3BlobStore.from_url(fresh_blob_url("sources"))
    yield lambda name: SourceStore(tmp_path / name / "data", blobs=blobs, cache_dir=tmp_path / name / "cache")
    blobs.delete([info.key for info in blobs.list()])


def _uploaded(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_store_read_list_and_checkout(replicas: Replicas, tmp_path: Path) -> None:
    writer, reader = replicas("writer"), replicas("reader")
    up = _uploaded(tmp_path / "up", {"pkg/a.py": "one", "pkg/ü b.py": "two", "secret.txt": "no"})
    (up / "pkg" / "link.py").symlink_to(up / "pkg" / "a.py")
    result = writer.store(3, SHA, up, allowed={"pkg/a.py", "pkg/ü b.py", "pkg/link.py"})
    assert sorted(result.stored) == ["pkg/a.py", "pkg/ü b.py"]
    assert result.refused == {"not_in_bundle": 1, "not_regular": 1}
    assert reader.has(3, SHA) and not reader.has(3, OTHER)
    assert reader.list(3, SHA) == ["pkg/a.py", "pkg/ü b.py"]
    assert reader.read(3, SHA, "pkg/ü b.py") == b"two"
    assert reader.read(3, SHA, "pkg/missing.py") is None
    assert reader.read(3, SHA, "../x") is None and reader.read(3, "HEAD", "pkg/a.py") is None
    checkout = reader.checkout(3, SHA)
    assert (checkout / "pkg" / "a.py").read_text() == "one"
    if reader.blobs is not None:
        assert checkout == commit_dir(reader.cache_dir, 3, SHA), "a replica reads its cache"
        cached = checkout / "pkg" / "a.py"
        assert stat.S_IMODE(cached.stat().st_mode) == 0o600
        assert stat.S_IMODE(cached.parent.stat().st_mode) == 0o700


def test_the_first_upload_of_a_path_wins_on_every_replica(replicas: Replicas, tmp_path: Path) -> None:
    first, second = replicas("first"), replicas("second")
    first.store(3, SHA, _uploaded(tmp_path / "one", {"a.py": "original"}), allowed={"a.py"})
    again = second.store(
        3, SHA, _uploaded(tmp_path / "two", {"a.py": "tampered", "b.py": "new"}), allowed={"a.py", "b.py"}
    )
    assert again.kept == ["a.py"] and again.stored == ["b.py"]
    assert second.read(3, SHA, "a.py") == b"original" and first.read(3, SHA, "b.py") == b"new"


def test_a_cached_symlink_is_never_followed(replicas: Replicas, tmp_path: Path) -> None:
    store = replicas("reader")
    if store.blobs is None:
        pytest.skip("the local default's own symlink rules are tested in tests/web/test_uploads.py")
    replicas("writer").store(3, SHA, _uploaded(tmp_path / "up", {"a.py": "one"}), allowed={"a.py"})
    target = commit_dir(store.cache_dir, 3, SHA)
    target.mkdir(parents=True)
    (tmp_path / "elsewhere").write_text("secret")
    (target / "a.py").symlink_to(tmp_path / "elsewhere")
    assert store.read(3, SHA, "a.py") is None
    assert (tmp_path / "elsewhere").read_text() == "secret"


def _count(store: SourceStore, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the store requests a replica makes."""
    assert store.blobs is not None
    calls = {"head": 0, "get": 0, "list": 0}
    blobs = store.blobs
    head, get_file, listing = blobs.head, blobs.get_file, blobs.list

    def counting_head(key: str) -> object:
        calls["head"] += 1
        return head(key)

    def counting_get(key: str, path: Path, **kwargs: object) -> str:
        calls["get"] += 1
        return get_file(key, path, **kwargs)  # type: ignore[arg-type]

    def counting_list(prefix: str = "") -> object:
        calls["list"] += 1
        return listing(prefix)

    monkeypatch.setattr(blobs, "head", counting_head)
    monkeypatch.setattr(blobs, "get_file", counting_get)
    monkeypatch.setattr(blobs, "list", counting_list)
    return calls


def test_checkout_is_one_listing_and_parallel_gets(
    replicas: Replicas, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, reader = replicas("writer"), replicas("reader")
    if reader.blobs is None:
        pytest.skip("the local default has nothing to fetch")
    files = {f"pkg/m{n}.py": f"module {n}" for n in range(20)}
    writer.store(3, SHA, _uploaded(tmp_path / "up", files), allowed=set(files))
    writer.blobs.put_bytes(f"sources/3/{SHA}/pkg/huge.py", b"x" * (MAX_SOURCE_FILE_BYTES + 1))  # type: ignore[union-attr]
    calls = _count(reader, monkeypatch)
    checkout = reader.checkout(3, SHA)
    assert sorted(p.name for p in (checkout / "pkg").iterdir()) == sorted(f"m{n}.py" for n in range(20))
    assert calls == {"head": 0, "get": 20, "list": 1}, "sizes come from the listing; nothing is HEADed"
    assert reader.checkout(3, SHA) == checkout and calls["get"] == 20, "a second checkout fetches nothing"


def test_a_miss_is_remembered_for_the_sync_interval(
    replicas: Replicas, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, reader = replicas("writer"), replicas("reader")
    if reader.blobs is None:
        pytest.skip("the local default has no bucket to ask")
    writer.store(3, SHA, _uploaded(tmp_path / "up", {"a.py": "one"}), allowed={"a.py"})
    cache.configure({cache.SYNC_INTERVAL_ENV: "3600"})
    calls = _count(reader, monkeypatch)
    assert reader.read(3, SHA, "a.py") == b"one" and calls == {"head": 0, "get": 1, "list": 0}
    assert reader.read(3, SHA, "a.py") == b"one" and calls["get"] == 1, "a hit reads the cache"
    assert reader.read(3, SHA, "never.py") is None and calls["get"] == 2
    assert reader.read(3, SHA, "never.py") is None and calls["get"] == 2, "a miss is not asked again"
    cache.configure({cache.SYNC_INTERVAL_ENV: "0"})
    assert reader.read(3, SHA, "never.py") is None and calls["get"] == 3, "until the interval passes"
    cache.configure({cache.SYNC_INTERVAL_ENV: "3600"})
    assert reader.read(3, SHA, "later.py") is None and calls["get"] == 4
    reader.store(3, SHA, _uploaded(tmp_path / "more", {"later.py": "two"}), allowed={"later.py"})
    assert reader.read(3, SHA, "later.py") == b"two", "a store on this replica forgets the misses"
    cache.configure({cache.SYNC_INTERVAL_ENV: "0"})


def test_retention_and_purge_remove_objects_and_cached_copies(replicas: Replicas, tmp_path: Path) -> None:
    writer, reader = replicas("writer"), replicas("reader")
    for sha in (SHA, OTHER):
        writer.store(3, sha, _uploaded(tmp_path / sha, {"a.py": sha}), allowed={"a.py"})
    assert reader.read(3, SHA, "a.py") == SHA.encode()
    assert writer.remove(3, [SHA, "not-a-sha"], dry_run=True) == [SHA] and reader.has(3, SHA)
    assert reader.remove(3, [SHA]) == [SHA]
    assert not writer.has(3, SHA) and writer.read(3, SHA, "a.py") is None
    assert not commit_dir(reader.cache_dir, 3, SHA).exists()
    writer.purge(3)
    assert not reader.has(3, OTHER) and reader.list(3, OTHER) == []
