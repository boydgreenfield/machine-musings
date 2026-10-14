"""Dense per-repo ids for paths, test nodeids and shape keys (``repo=<id>/dictionary.json``).

Ids are list positions, so they are stable once assigned and only ever appended. Test id 0 is
reserved for ``<import>``. Extensions happen under an exclusive ``flock`` on ``dictionary.lock`` and
are written atomically, so concurrent ingests of the same repo (in-process or from the CLI) never
hand out the same id twice. Garbage collection never touches the dictionary.

**Over an object store** (the repo directory is a cache of a bucket:
:func:`coverset.storage.facts_remote`), the object ``facts/repo=<id>/dictionary.json`` is the
dictionary and the local file only its cached copy. :meth:`RepoDictionary.extend` is then a
compare-and-swap loop: GET the object and its etag, append the new values, PUT with ``If-Match`` on
that etag (``If-None-Match: *`` for the first version); when another replica got there first the
PUT fails with :class:`~coverset.storage.base.PreconditionFailed` and the loop starts again from the
newer version, after a short random back-off. Two replicas therefore never hand out the same id:
an id is only ever used after the PUT that assigned it succeeded. The local ``flock`` is still held
around the loop, so processes on one machine queue instead of racing. :meth:`RepoDictionary.pull`
refreshes the cached copy for readers; because the lists only grow, the copy is replaced only by a
version with at least as many entries (a slow reader never puts back an older one), and its etag
is kept in the hidden ``.dictionary.sync`` beside it.
"""

from __future__ import annotations

import fcntl
import json
import random
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coverset.facts.atomic import atomic_write_bytes
from coverset.lang.base import IMPORT_CONTEXT

if TYPE_CHECKING:
    from coverset.storage.base import BlobStore

__all__ = ["DICTIONARY_FILE", "DictionaryConflict", "RepoDictionary"]

DICTIONARY_FILE = "dictionary.json"
_LOCK_FILE = "dictionary.lock"
_SYNC_FILE = ".dictionary.sync"
_VERSION = 1
CAS_ATTEMPTS = 50
"""Compare-and-swap rounds before :class:`DictionaryConflict` (each loses only to a successful
writer, so 50 means 50 other ingests landed in between)."""


class DictionaryConflict(RuntimeError):
    """The dictionary kept changing under :meth:`RepoDictionary.extend` (object store only)."""


def remote_key(repo_dir: Path) -> str:
    """The dictionary's object key: ``facts/repo=<id>/dictionary.json``."""
    return f"facts/{repo_dir.name}/{DICTIONARY_FILE}"


def _size(data: dict[str, Any]) -> int:
    return sum(len(data.get(name) or ()) for name in ("paths", "nodeids", "shapes"))


def _store_local(repo_dir: Path, payload: bytes, etag: str, size: int) -> None:
    """Replace the cached copy (caller holds the repo lock) unless it already has more entries."""
    try:
        current = json.loads((repo_dir / _SYNC_FILE).read_bytes())
    except (FileNotFoundError, ValueError):
        current = {}
    if isinstance(current, dict) and int(current.get("size") or 0) > size:
        return
    atomic_write_bytes(repo_dir / DICTIONARY_FILE, payload)
    atomic_write_bytes(repo_dir / _SYNC_FILE, json.dumps({"etag": etag, "size": size}).encode())


@contextmanager
def _repo_lock(repo_dir: Path) -> Iterator[None]:
    repo_dir.mkdir(parents=True, exist_ok=True)
    with open(repo_dir / _LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@dataclass(slots=True)
class _Ids:
    """One append-only id space: ``values[id]`` and its reverse index."""

    values: list[str]
    index: dict[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.index = {value: i for i, value in enumerate(self.values)}

    def extend(self, new: Iterable[str]) -> bool:
        changed = False
        for value in new:
            if value not in self.index:
                self.index[value] = len(self.values)
                self.values.append(value)
                changed = True
        return changed


@dataclass(slots=True)
class RepoDictionary:
    """A snapshot of one repo's id spaces; :meth:`extend` grows it durably."""

    repo_dir: Path
    _paths: _Ids
    _nodeids: _Ids
    _shapes: _Ids

    @classmethod
    def load(cls, repo_dir: Path) -> RepoDictionary:
        """Read the dictionary of ``repo_dir`` (empty, with ``<import>`` as test 0, if absent)."""
        try:
            data = json.loads((repo_dir / DICTIONARY_FILE).read_bytes())
        except FileNotFoundError:
            data = {}
        return cls._from_data(repo_dir, data)

    @classmethod
    def _from_data(cls, repo_dir: Path, data: dict[str, Any]) -> RepoDictionary:
        nodeids = data.get("nodeids") or [IMPORT_CONTEXT]
        if nodeids[0] != IMPORT_CONTEXT:
            raise ValueError(f"{repo_dir / DICTIONARY_FILE}: test id 0 must be {IMPORT_CONTEXT!r}")
        return cls(
            repo_dir=repo_dir,
            _paths=_Ids(list(data.get("paths", []))),
            _nodeids=_Ids(list(nodeids)),
            _shapes=_Ids(list(data.get("shapes", []))),
        )

    @classmethod
    def extend(
        cls,
        repo_dir: Path,
        *,
        paths: Iterable[str] = (),
        nodeids: Iterable[str] = (),
        shapes: Iterable[str] = (),
    ) -> RepoDictionary:
        """Assign ids to any new values (under the repo lock) and return the updated dictionary.

        Over an object store this is the compare-and-swap loop of the module docstring.
        """
        from coverset.storage import facts_remote

        blobs = facts_remote(repo_dir.parent)
        if blobs is not None:
            return cls._extend_remote(repo_dir, blobs, paths=paths, nodeids=nodeids, shapes=shapes)
        with _repo_lock(repo_dir):
            current = cls.load(repo_dir)
            changed = current._paths.extend(sorted(set(paths)))
            changed |= current._nodeids.extend(sorted(set(nodeids)))
            changed |= current._shapes.extend(sorted(set(shapes)))
            if changed:
                payload = {
                    "version": _VERSION,
                    "paths": current._paths.values,
                    "nodeids": current._nodeids.values,
                    "shapes": current._shapes.values,
                }
                atomic_write_bytes(repo_dir / DICTIONARY_FILE, json.dumps(payload).encode())
            return current

    def _payload(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "paths": self._paths.values,
            "nodeids": self._nodeids.values,
            "shapes": self._shapes.values,
        }

    @classmethod
    def _extend_remote(
        cls,
        repo_dir: Path,
        blobs: BlobStore,
        *,
        paths: Iterable[str],
        nodeids: Iterable[str],
        shapes: Iterable[str],
    ) -> RepoDictionary:
        from coverset.storage.base import BlobNotFound, PreconditionFailed

        new_paths, new_nodeids, new_shapes = sorted(set(paths)), sorted(set(nodeids)), sorted(set(shapes))
        key = remote_key(repo_dir)
        with _repo_lock(repo_dir):
            for attempt in range(CAS_ATTEMPTS):
                try:
                    raw, etag = blobs.get_bytes(key)
                    data = json.loads(raw)
                except BlobNotFound:
                    raw, etag, data = b"", None, {}
                current = cls._from_data(repo_dir, data)
                changed = current._paths.extend(new_paths)
                changed |= current._nodeids.extend(new_nodeids)
                changed |= current._shapes.extend(new_shapes)
                if not changed:
                    if etag is not None:
                        _store_local(repo_dir, raw, etag, _size(data))
                    return current
                payload = current._payload()
                encoded = json.dumps(payload).encode()
                try:
                    if etag is None:
                        written = blobs.put_bytes(key, encoded, if_none_match=True)
                    else:
                        written = blobs.put_bytes(key, encoded, if_match=etag)
                except PreconditionFailed:
                    time.sleep(random.uniform(0, min(0.5, 0.005 * 2**attempt)))
                    continue
                _store_local(repo_dir, encoded, written, _size(payload))
                return current
        raise DictionaryConflict(f"{key}: still changing after {CAS_ATTEMPTS} compare-and-swap rounds")

    @staticmethod
    def pull(repo_dir: Path, blobs: BlobStore) -> None:
        """Refresh the cached copy from the object store when its etag changed (readers).

        A dictionary gone from the bucket (the repository was purged) takes its cached copy with it.
        """
        key = remote_key(repo_dir)
        info = blobs.head(key)
        try:
            seen = json.loads((repo_dir / _SYNC_FILE).read_bytes()).get("etag")
        except (FileNotFoundError, ValueError, AttributeError):
            seen = None
        if info is None:
            if seen is not None:
                with _repo_lock(repo_dir):
                    (repo_dir / DICTIONARY_FILE).unlink(missing_ok=True)
                    (repo_dir / _SYNC_FILE).unlink(missing_ok=True)
            return
        if info.etag == seen:
            return
        raw, etag = blobs.get_bytes(key)
        data = json.loads(raw)
        with _repo_lock(repo_dir):
            _store_local(repo_dir, raw, etag, _size(data))

    def file_id(self, path: str) -> int:
        """The id of ``path`` (KeyError if unassigned)."""
        return self._paths.index[path]

    def test_id(self, nodeid: str) -> int:
        """The id of ``nodeid`` (KeyError if unassigned)."""
        return self._nodeids.index[nodeid]

    def shape_id(self, shape_key: str) -> int:
        """The id of ``shape_key`` (KeyError if unassigned)."""
        return self._shapes.index[shape_key]

    def find_file_id(self, path: str) -> int | None:
        """The id of ``path``, or None when the repo has never seen it."""
        return self._paths.index.get(path)

    def find_test_id(self, nodeid: str) -> int | None:
        """The id of ``nodeid``, or None when the repo has never seen it."""
        return self._nodeids.index.get(nodeid)

    def path(self, file_id: int) -> str:
        """The path with id ``file_id``."""
        return self._paths.values[file_id]

    def nodeid(self, test_id: int) -> str:
        """The nodeid with id ``test_id``."""
        return self._nodeids.values[test_id]

    def shape_key(self, shape_id: int) -> str:
        """The shape key with id ``shape_id``."""
        return self._shapes.values[shape_id]
