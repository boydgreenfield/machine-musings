"""The facts bundle a worker hands back: one ``bundle.tar.gz`` with every file the ingest reads.

Members (flat): ``coverage.sqlite``, ``coverage.json``, ``shapes.jsonl.gz``, ``results.jsonl``,
``collect.json``, ``durations.json``, ``invariant_links.jsonl`` and ``meta.json``. Paths inside are
git-root-relative POSIX; test ids are pytest nodeids.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pytest_coverset.outcomes import TestResult
from pytest_coverset.paths import norm_path
from pytest_coverset.shapes import SHAPES_FORMAT_VERSION

RESULTS_FILE = "results.jsonl"
COLLECT_FILE = "collect.json"
DURATIONS_FILE = "durations.json"
INVARIANT_LINKS_FILE = "invariant_links.jsonl"
SHAPES_FILE = "shapes.jsonl.gz"
META_FILE = "meta.json"
BUNDLE_FILE = "bundle.tar.gz"
BUNDLE_MEMBERS = (
    "coverage.sqlite",
    "coverage.json",
    SHAPES_FILE,
    RESULTS_FILE,
    COLLECT_FILE,
    DURATIONS_FILE,
    INVARIANT_LINKS_FILE,
    META_FILE,
)


@dataclass(frozen=True, slots=True)
class CollectedItem:
    """One collected test as recorded in ``collect.json``."""

    nodeid: str
    file: str
    line: int | None
    markers: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    hypothesis: bool

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready mapping."""
        return {
            "nodeid": self.nodeid,
            "file": self.file,
            "line": self.line,
            "markers": list(self.markers),
            "invariant_ids": list(self.invariant_ids),
            "hypothesis": self.hypothesis,
        }


def invariant_ids(item: pytest.Item) -> tuple[str, ...]:
    """Invariant ids from ``@pytest.mark.invariant("id")`` (or ``id=``) markers, closest first, unique."""
    found: list[str] = []
    for mark in item.iter_markers("invariant"):
        ident = mark.args[0] if mark.args else mark.kwargs.get("id")
        if isinstance(ident, str) and ident and ident not in found:
            found.append(ident)
    return tuple(found)


def describe_item(item: pytest.Item, repo_root: str) -> CollectedItem:
    """Describe a collected item for ``collect.json``."""
    rel = norm_path(item.path, repo_root)
    _, line, _ = item.location
    obj = getattr(item, "obj", None)
    is_hypothesis = (
        bool(getattr(obj, "is_hypothesis_test", False)) or item.get_closest_marker("hypothesis") is not None
    )
    return CollectedItem(
        nodeid=item.nodeid,
        file=rel if rel is not None else item.location[0],
        line=line + 1 if line is not None else None,
        markers=tuple(sorted({m.name for m in item.iter_markers()})),
        invariant_ids=invariant_ids(item),
        hypothesis=is_hypothesis,
    )


def hypothesis_info() -> dict[str, Any] | None:
    """Hypothesis version and the default profile's deadline (ms), or None when it is not installed."""
    try:
        import hypothesis
    except ImportError:
        return None
    deadline = hypothesis.settings.default.deadline
    return {
        "version": hypothesis.__version__,
        "deadline_ms": deadline.total_seconds() * 1000 if deadline is not None else None,
    }


def dumps(obj: Any) -> str:
    """Compact ASCII JSON: a lone surrogate or any odd code point is escaped, never an encode error."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def write_json(path: Path, obj: Any) -> None:
    """Write compact JSON atomically (temp file + rename); no ``.tmp`` file is left behind on error."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(dumps(obj), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Write JSON lines atomically (temp file + rename); no ``.tmp`` file is left behind on error."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as out:
            for row in rows:
                out.write(dumps(row) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_empty_shapes(path: Path) -> None:
    """Write a header-only ``shapes.jsonl.gz`` (shapes off or unavailable)."""
    header = {
        "kind": "header",
        "version": SHAPES_FORMAT_VERSION,
        "funcs": [],
        "shapes": [],
        "tests": [],
        "callers": [],
    }
    with gzip.open(path, "wt", encoding="utf-8") as out:
        out.write(json.dumps(header, separators=(",", ":")) + "\n")


class ResultsLog:
    """Appends final test rows to ``results.jsonl`` as tests finish and keeps them for the bundle."""

    def __init__(self, out_dir: Path) -> None:
        self.path = out_dir / RESULTS_FILE
        self._file = self.path.open("w", encoding="utf-8")
        self.results: list[TestResult] = []

    def add(self, result: TestResult) -> None:
        """Record one result (kept even if the write fails) and flush it to disk."""
        self.results.append(result)
        if not self._file.closed:
            self._file.write(dumps(result.as_dict()) + "\n")
            self._file.flush()

    def close(self) -> None:
        """Close the file (idempotent)."""
        self._file.close()

    def counts(self, collected: int) -> dict[str, int]:
        """Outcome counts plus ``not_run`` (collected items with no result)."""
        counts = dict.fromkeys(("passed", "failed", "error", "skipped", "xfailed", "xpassed"), 0)
        for result in self.results:
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
        counts["not_run"] = max(0, collected - len({r.nodeid for r in self.results}))
        return counts


def write_collect(
    out_dir: Path, collected: Iterable[CollectedItem], errors: Iterable[Mapping[str, Any]]
) -> None:
    """Write ``collect.json``."""
    write_json(
        out_dir / COLLECT_FILE, {"items": [item.as_dict() for item in collected], "errors": list(errors)}
    )


def write_durations(out_dir: Path, results: Iterable[TestResult]) -> None:
    """Write ``durations.json``."""
    write_json(out_dir / DURATIONS_FILE, {r.nodeid: r.duration_s for r in results})


def write_invariant_links(out_dir: Path, collected: Iterable[CollectedItem]) -> None:
    """Write ``invariant_links.jsonl``."""
    write_jsonl(
        out_dir / INVARIANT_LINKS_FILE,
        ({"test_id": item.nodeid, "invariant_id": inv} for item in collected for inv in item.invariant_ids),
    )


def write_static_files(
    out_dir: Path,
    collected: Iterable[CollectedItem],
    collect_errors: Iterable[Mapping[str, Any]],
    results: Iterable[TestResult],
) -> list[tuple[str, Exception]]:
    """Write ``collect.json``, ``durations.json`` and ``invariant_links.jsonl``, each independently.

    A failure in one file does not stop the others; the failures are returned as ``(file, error)``.
    """
    items = list(collected)
    failures: list[tuple[str, Exception]] = []
    for name, action in (
        (COLLECT_FILE, lambda: write_collect(out_dir, items, collect_errors)),
        (DURATIONS_FILE, lambda: write_durations(out_dir, results)),
        (INVARIANT_LINKS_FILE, lambda: write_invariant_links(out_dir, items)),
    ):
        try:
            action()
        except Exception as exc:
            failures.append((name, exc))
    return failures


def _clean_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def make_tarball(out_dir: Path) -> tuple[Path, str, int]:
    """Pack every present bundle member into ``bundle.tar.gz`` (streamed to disk, then renamed atomically).

    Returns the tarball path, its sha256 and its size in bytes.
    """
    target = out_dir / BUNDLE_FILE
    tmp = target.with_name(target.name + ".tmp")
    try:
        with (
            tmp.open("wb") as raw,
            gzip.GzipFile(filename=BUNDLE_FILE, mode="wb", fileobj=raw) as compressed,
            tarfile.open(fileobj=compressed, mode="w") as tar,
        ):
            for name in BUNDLE_MEMBERS:
                member = out_dir / name
                if member.is_file():
                    tar.add(member, arcname=name, recursive=False, filter=_clean_tarinfo)
        with tmp.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        size = tmp.stat().st_size
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target, digest, size


def python_info() -> dict[str, str]:
    """Interpreter version and implementation for ``meta.json``."""
    return {"version": sys.version.split()[0], "implementation": sys.implementation.name}
