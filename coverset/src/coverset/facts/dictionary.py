"""Dense per-repo ids for paths, test nodeids and shape keys (``repo=<id>/dictionary.json``).

Ids are list positions, so they are stable once assigned and only ever appended. Test id 0 is
reserved for ``<import>``. Extensions happen under an exclusive ``flock`` on ``dictionary.lock`` and
are written atomically, so concurrent ingests of the same repo (in-process or from the CLI) never
hand out the same id twice. Garbage collection never touches the dictionary.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from coverset.facts.atomic import atomic_write_bytes
from coverset.lang.base import IMPORT_CONTEXT

__all__ = ["DICTIONARY_FILE", "RepoDictionary"]

DICTIONARY_FILE = "dictionary.json"
_LOCK_FILE = "dictionary.lock"
_VERSION = 1


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
        """Assign ids to any new values (under the repo lock) and return the updated dictionary."""
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
