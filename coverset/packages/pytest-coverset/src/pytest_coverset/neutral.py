"""The language-neutral bundle (``docs/neutral-bundle.md`` in coverset), written next to the Python one.

With ``--coverset-neutral`` (or ``COVERSET_NEUTRAL=1``) the session writes ``<out>/neutral/`` and
``<out>/neutral.tar.gz`` (the same files, flat) after the Python bundle files exist. Everything is
derived from files already on disk or rows already recorded, so the export cannot change coverage,
shapes or results:

* ``tests.jsonl.gz``: every collected test (``outcome`` null when it never ran) merged with its
  final result; results for tests that were not collected are appended.
* ``files.jsonl.gz`` and ``arcs.jsonl.gz``: ``coverage.json``'s statements (executed ∪ missing),
  excluded lines, possible branch arcs (executed ∪ missing) and executed arcs per file.
  ``n_lines`` is the source file's line count (the largest line mentioned when it is unreadable).
* ``coverage.jsonl.gz``: per (context, file), from ``coverage.sqlite`` through coverage.py's public
  :class:`coverage.CoverageData` API (``contexts_by_lineno`` to find the pairs,
  ``set_query_context`` + ``arcs``/``lines`` per pair). Raw tracer lines are mapped to the first line
  of their statement, exactly as the control plane's ``read_python_bundle`` does, and only
  statement lines are kept; the ``''`` context is ``<import>``. Rows carry the pair's ``arcs`` when
  the data has arcs.
* ``functions.jsonl.gz``: :func:`pytest_coverset.regions.index_functions` over every measured
  ``.py`` file (no ``types``).
* ``shapes.jsonl.gz``: the header-indexed rows of ``shapes.jsonl.gz`` as self-contained ``obs``
  and ``sampling`` rows. Observations made at import time (caller ``<import>``) are left out, as
  the control plane's Python reader leaves them out.
* ``links.jsonl``: the invariant links.

``manifest.json`` is written last, with ``meta`` the same mapping as ``meta.json``.
"""

from __future__ import annotations

import bisect
import gzip
import hashlib
import json
import os
import re
import tarfile
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import coverage

from pytest_coverset.bundle import CollectedItem
from pytest_coverset.outcomes import TestResult
from pytest_coverset.paths import norm_path
from pytest_coverset.regions import index_functions

FORMAT = "coverset-neutral"
VERSION = 1
LANGUAGE = "python"
PRODUCER = "pytest-coverset"
NEUTRAL_DIR = "neutral"
NEUTRAL_TARBALL = "neutral.tar.gz"
MANIFEST_FILE = "manifest.json"
TESTS_FILE = "tests.jsonl.gz"
FILES_FILE = "files.jsonl.gz"
COVERAGE_FILE = "coverage.jsonl.gz"
ARCS_FILE = "arcs.jsonl.gz"
FUNCTIONS_FILE = "functions.jsonl.gz"
SHAPES_FILE = "shapes.jsonl.gz"
LINKS_FILE = "links.jsonl"
DATA_FILES = (TESTS_FILE, FILES_FILE, COVERAGE_FILE, ARCS_FILE, FUNCTIONS_FILE, SHAPES_FILE, LINKS_FILE)
MEMBERS = (MANIFEST_FILE, *DATA_FILES)
IMPORT_CONTEXT = "<import>"
NEUTRAL_ENV = "COVERSET_NEUTRAL"
SHARD_ENV = "COVERSET_SHARD"
MAX_REPR_CHARS = 200
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_LINE = 1_000_000
_TRUE = frozenset({"1", "true", "yes", "on"})
_SHARD = re.compile(r"[0-9]{1,7}")


def env_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``COVERSET_NEUTRAL`` asks for the neutral bundle (``1``, ``true``, ``yes``, ``on``)."""
    value = (os.environ if environ is None else environ).get(NEUTRAL_ENV, "")
    return value.strip().lower() in _TRUE


def shard_from_env(environ: Mapping[str, str] | None = None) -> int | None:
    """``COVERSET_SHARD`` as a shard index (0..1,000,000), or None when unset or not a number."""
    value = (os.environ if environ is None else environ).get(SHARD_ENV, "").strip()
    if not _SHARD.fullmatch(value) or int(value) > MAX_LINE:
        return None
    return int(value)


def utc_now() -> str:
    """The current time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _line(row: Mapping[str, Any]) -> bytes:
    """One UTF-8 JSON line; a lone surrogate becomes ``?`` (the format forbids them)."""
    text = json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8", errors="replace") + b"\n"


def _write_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    """Write JSON lines (gzip when the name ends in ``.gz``) atomically; return the row count."""
    tmp = path.with_name(path.name + ".tmp")
    count = 0
    try:
        with tmp.open("wb") as raw:
            if path.name.endswith(".gz"):
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as gz:
                    for row in rows:
                        gz.write(_line(row))
                        count += 1
            else:
                for row in rows:
                    raw.write(_line(row))
                    count += 1
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return count


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


@dataclass(frozen=True, slots=True)
class _Static:
    """One file of ``coverage.json``: the ``files.jsonl`` row plus what coverage rows need."""

    path: str
    n_lines: int
    statements: tuple[int, ...]
    excluded: tuple[int, ...]
    branches: tuple[tuple[int, int], ...]
    executed_arcs: tuple[tuple[int, int], ...]
    anchors: tuple[int, ...] = ()
    anchor_is_statement: tuple[bool, ...] = ()

    def first_lines(self, lines: Iterable[int]) -> list[int]:
        """Map raw executed lines to their statement's first line; keep statement lines only.

        This is ``coverset.facts.convert.first_lines`` (coverage.py's ``translate_lines`` without
        re-parsing): a line that is not itself a statement belongs to the statement that starts
        closest above it, and a line inside an excluded statement keeps its number.
        """
        statements = set(self.statements)
        mapped: set[int] = set()
        for line in lines:
            if line <= 0:
                continue
            index = bisect.bisect_right(self.anchors, line) - 1
            if index >= 0 and self.anchor_is_statement[index]:
                line = self.anchors[index]
            if line in statements and line <= self.n_lines:
                mapped.add(line)
        return sorted(mapped)

    def row(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "n_lines": self.n_lines,
            "statements": list(self.statements),
            "excluded": list(self.excluded),
            "branches": [list(arc) for arc in self.branches],
        }


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= MAX_LINE]


def _arc_list(value: object) -> set[tuple[int, int]]:
    if not isinstance(value, list):
        return set()
    found: set[tuple[int, int]] = set()
    for arc in value:
        if (
            isinstance(arc, list)
            and len(arc) == 2
            and all(type(v) is int and abs(v) <= MAX_LINE for v in arc)
        ):
            found.add((arc[0], arc[1]))
    return found


@dataclass(slots=True)
class NeutralWriter:
    """Writes one session's neutral bundle into ``<out_dir>/neutral``."""

    out_dir: Path
    repo_root: str
    cwd: str
    """The working directory when coverage started: ``coverage.json``/``coverage.sqlite`` paths are
    relative to it."""
    stats: Counter[str] = field(default_factory=Counter)
    rows: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def directory(self) -> Path:
        return self.out_dir / NEUTRAL_DIR

    def rel(self, raw: str) -> str | None:
        """``raw`` (absolute or relative to :attr:`cwd`) as a repo-root-relative POSIX path."""
        return norm_path(os.path.join(self.cwd, raw), self.repo_root)

    # -- data files ----------------------------------------------------------------------------

    def write_data(
        self,
        collected: Iterable[CollectedItem],
        results: Iterable[TestResult],
        coverage_json: Path,
        coverage_sqlite: Path,
        shapes_file: Path,
    ) -> None:
        """Write every data file (not the manifest); a failure in one file does not stop the others.

        Raises the first failure after attempting all of them.
        """
        started = time.perf_counter()
        target = self.directory
        target.mkdir(parents=True, exist_ok=True)
        for name in (*MEMBERS, NEUTRAL_TARBALL):
            (target / name).unlink(missing_ok=True)
        items = list(collected)
        static = self._static(coverage_json)
        failures: list[BaseException] = []
        writers = (
            (TESTS_FILE, lambda: self._test_rows(items, results)),
            (FILES_FILE, lambda: (s.row() for s in static.values())),
            (COVERAGE_FILE, lambda: self._coverage_rows(coverage_sqlite, static)),
            (
                ARCS_FILE,
                lambda: (
                    {"file": s.path, "arcs": [list(a) for a in s.executed_arcs]} for s in static.values()
                ),
            ),
            (FUNCTIONS_FILE, lambda: self._function_rows(static)),
            (SHAPES_FILE, lambda: self._shape_rows(shapes_file)),
            (
                LINKS_FILE,
                lambda: ({"test": i.nodeid, "invariant": inv} for i in items for inv in i.invariant_ids),
            ),
        )
        for name, rows in writers:
            try:
                self.rows[name] = _write_rows(target / name, rows())
            except Exception as exc:
                failures.append(exc)
                (target / name).unlink(missing_ok=True)
        self.seconds += time.perf_counter() - started
        if failures:
            raise failures[0]

    def _test_rows(
        self, items: list[CollectedItem], results: Iterable[TestResult]
    ) -> Iterator[dict[str, Any]]:
        final: dict[str, TestResult] = {}
        for result in results:
            final[result.nodeid] = result  # the last row for a nodeid wins, as in results.jsonl
        seen: set[str] = set()
        for item in items:
            if item.nodeid in seen:
                continue
            seen.add(item.nodeid)
            yield _test_row(item.nodeid, item.file, item.line, item, final.get(item.nodeid))
        for nodeid, result in final.items():
            if nodeid not in seen:
                yield _test_row(nodeid, result.file, None, None, result)

    def _static(self, coverage_json: Path) -> dict[str, _Static]:
        """Per-file static facts from ``coverage.json`` (empty when it is missing or unreadable)."""
        try:
            report = json.loads(coverage_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.stats["coverage_json_unreadable"] += 1
            return {}
        files = report.get("files") if isinstance(report, dict) else None
        found: dict[str, _Static] = {}
        for raw, entry in (files or {}).items() if isinstance(files, dict) else ():
            path = self.rel(raw) if isinstance(raw, str) else None
            if path is None or not isinstance(entry, dict) or path in found:
                self.stats["files_dropped"] += 1
                continue
            statements = sorted(
                set(_int_list(entry.get("executed_lines"))) | set(_int_list(entry.get("missing_lines")))
            )
            excluded = sorted(set(_int_list(entry.get("excluded_lines"))))
            executed = _arc_list(entry.get("executed_branches"))
            branches = sorted(executed | _arc_list(entry.get("missing_branches")))
            mentioned = [*statements, *excluded, *(line for arc in branches for line in arc)]
            n_lines = self._n_lines(path, max(mentioned, default=0))
            anchors = sorted(set(statements) | set(excluded))
            stmt_set = set(statements)
            found[path] = _Static(
                path=path,
                n_lines=n_lines,
                statements=tuple(statements),
                excluded=tuple(excluded),
                branches=tuple(branches),
                executed_arcs=tuple(sorted(executed)),
                anchors=tuple(anchors),
                anchor_is_statement=tuple(a in stmt_set for a in anchors),
            )
        return found

    def _n_lines(self, path: str, fallback: int) -> int:
        """Line count of the source file (``\\n``-separated, a final unterminated line counts)."""
        try:
            with open(os.path.join(self.repo_root, path), "rb") as fh:
                data = fh.read(MAX_SOURCE_BYTES + 1)
        except OSError:
            return fallback
        if len(data) > MAX_SOURCE_BYTES:
            return fallback
        return min(data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), MAX_LINE)

    def _coverage_rows(self, sqlite_path: Path, static: dict[str, _Static]) -> Iterator[dict[str, Any]]:
        """Rows per (context, file): ``<import>`` first, then contexts and paths sorted."""
        if not sqlite_path.is_file():
            self.stats["coverage_sqlite_missing"] += 1
            return
        data = coverage.CoverageData(basename=str(sqlite_path), suffix=False, no_disk=False)
        data.read()  # marks the data as used: a CoverageData never read would erase the file
        has_arcs = data.has_arcs()
        names: dict[str, _Static] = {}
        for raw in data.measured_files():
            path = self.rel(raw)
            if path is None or path not in static:
                self.stats["coverage_files_not_in_files"] += 1
            else:
                names[raw] = static[path]
        pairs: dict[str, dict[str, set[int]]] = defaultdict(dict)
        data.set_query_contexts(None)
        for raw in names:
            for line, contexts in data.contexts_by_lineno(raw).items():
                for context in contexts:
                    pairs[context].setdefault(raw, set()).add(line)
        try:
            for context in sorted(pairs, key=lambda c: (c != "", c)):
                if has_arcs:
                    data.set_query_context(context)
                for raw in sorted(pairs[context], key=lambda r: names[r].path):
                    facts = names[raw]
                    arcs: list[tuple[int, int]] = []
                    if has_arcs:
                        arcs = sorted(set(data.arcs(raw) or ()))
                        raw_lines: Iterable[int] = {n for arc in arcs for n in arc if n > 0}
                    else:
                        raw_lines = pairs[context][raw]
                    lines = facts.first_lines(raw_lines)
                    if not lines:
                        self.stats["coverage_rows_without_statements"] += 1
                        continue
                    row: dict[str, Any] = {
                        "test": context or IMPORT_CONTEXT,
                        "file": facts.path,
                        "lines": lines,
                    }
                    if has_arcs:
                        row["arcs"] = [list(arc) for arc in arcs]
                    yield row
        finally:
            data.set_query_contexts(None)

    def _function_rows(self, static: dict[str, _Static]) -> Iterator[dict[str, Any]]:
        for path in sorted(static):
            if not path.endswith(".py"):
                continue
            try:
                source = Path(self.repo_root, path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self.stats["functions_unreadable_sources"] += 1
                continue
            for region in index_functions(path, source):
                yield region.as_row()

    def _shape_rows(self, shapes_file: Path) -> Iterator[dict[str, Any]]:
        """``obs`` and ``sampling`` rows from the header-indexed ``shapes.jsonl.gz``."""
        if not shapes_file.is_file():
            return
        with gzip.open(shapes_file, "rt", encoding="utf-8") as fh:
            header = json.loads(fh.readline() or "null")
            if not isinstance(header, dict) or header.get("kind") != "header":
                self.stats["shapes_bad_header"] += 1
                return
            table = _ShapeTable.of(header)
            for text in fh:
                row = json.loads(text)
                kind = row.get("kind")
                out = (
                    table.obs(row)
                    if kind == "obs"
                    else table.sampling(row)
                    if kind == "func_sampling"
                    else None
                )
                if out is None:
                    self.stats["shapes_rows_dropped"] += 1
                else:
                    yield out

    # -- manifest and archive ----------------------------------------------------------------

    def write_manifest(self, meta: Mapping[str, Any], *, version: str, shard: int | None) -> None:
        """Write ``manifest.json`` (atomically); ``meta`` is stored verbatim."""
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "language": LANGUAGE,
            "producer": {"name": PRODUCER, "version": version},
            "created_at": utc_now(),
            "shard": shard,
            "meta": dict(meta),
        }
        path = self.directory / MANIFEST_FILE
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_bytes(_line(manifest))
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def pack(self) -> tuple[Path, str, int]:
        """Pack the neutral files into ``<out>/neutral.tar.gz`` (flat); return path, sha256, bytes."""
        started = time.perf_counter()
        target = self.out_dir / NEUTRAL_TARBALL
        tmp = target.with_name(target.name + ".tmp")
        try:
            with (
                tmp.open("wb") as raw,
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
                tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar,
            ):
                for name in MEMBERS:
                    member = self.directory / name
                    if member.is_file() and not member.is_symlink():
                        tar.add(member, arcname=name, recursive=False, filter=_clean_tarinfo)
            with tmp.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            size = tmp.stat().st_size
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.seconds += time.perf_counter() - started
        return target, digest, size


def _clean_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.mode = 0o644
    return info


def _test_row(
    nodeid: str, file: str, line: int | None, item: CollectedItem | None, result: TestResult | None
) -> dict[str, Any]:
    return {
        "id": nodeid,
        "file": file,
        "line": line if isinstance(line, int) and 0 < line <= MAX_LINE else None,
        "outcome": result.outcome if result else None,
        "duration_s": max(result.duration_s, 0.0) if result else None,
        "failed_phase": result.failed_phase if result else None,
        "fail_sig": result.fail_sig if result else None,
        "message": result.longrepr if result else None,
        "markers": list(item.markers) if item else [],
        "invariants": list(item.invariant_ids) if item else [],
        "property": item.hypothesis if item else False,
        "start": result.start if result else None,
        "stop": result.stop if result else None,
    }


def _index(table: list[Any], index: object) -> Any:
    if type(index) is int and 0 <= index < len(table):
        return table[index]
    return None


@dataclass(frozen=True, slots=True)
class _ShapeTable:
    """The decoded header dictionaries of ``shapes.jsonl.gz`` (None for an unusable entry)."""

    funcs: list[list[Any] | None]
    shapes: list[str | None]
    tests: list[str | None]
    callers: list[list[Any] | dict[str, str] | None]

    @classmethod
    def of(cls, header: dict[str, Any]) -> _ShapeTable:
        def lst(key: str) -> list[Any]:
            value = header.get(key)
            return value if isinstance(value, list) else []

        return cls(
            funcs=[_triple(f) for f in lst("funcs")],
            shapes=[s if isinstance(s, str) and s else None for s in lst("shapes")],
            tests=[t if isinstance(t, str) and t else None for t in lst("tests")],
            callers=[_caller(c) for c in lst("callers")],
        )

    def _tests(self, indexes: object) -> list[str] | None:
        if not isinstance(indexes, list):
            return None
        return list(dict.fromkeys(t for t in (_index(self.tests, i) for i in indexes) if t is not None))

    def obs(self, row: dict[str, Any]) -> dict[str, Any] | None:
        func = _index(self.funcs, row.get("f"))
        shape = _index(self.shapes, row.get("s"))
        caller = _index(self.callers, row.get("c"))
        tests = self._tests(row.get("t"))
        samples = row.get("n")
        param = row.get("p")
        if func is None or shape is None or caller is None or tests is None:
            return None
        if type(samples) is not int or samples < 0 or not isinstance(param, str) or not param:
            return None
        ext = row.get("ext")
        return {
            "kind": "obs",
            "func": func,
            "param": param,
            "shape": shape,
            "caller": caller,
            "samples": samples,
            "tests": tests,
            "truncated": row.get("trunc") is True,
            "example": _clip(row.get("ex"), MAX_REPR_CHARS),
            "example_test": _index(self.tests, ext) if ext is not None else None,
        }

    def sampling(self, row: dict[str, Any]) -> dict[str, Any] | None:
        func = _index(self.funcs, row.get("f"))
        calls = row.get("calls")
        tests = self._tests(row.get("tests"))
        if func is None or type(calls) is not int or calls < 0 or tests is None:
            return None
        truncated = row.get("truncated") is True
        cap = row.get("cap") if truncated and row.get("cap") in ("sample", "full") else None
        return {
            "kind": "sampling",
            "func": func,
            "calls": calls,
            "tests": tests,
            "truncated": truncated,
            "cap": cap,
        }


def _triple(value: object) -> list[Any] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    path, qualname, line = value
    if not isinstance(path, str) or not path or not isinstance(qualname, str) or not qualname:
        return None
    if type(line) is not int or not 0 < line <= MAX_LINE:
        return None
    return [path, qualname, line]


def _caller(value: object) -> list[Any] | dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    if value.get("kind") == "test":
        nodeid = value.get("nodeid")
        # An import-time call (a decorator applied while the module loads) has the caller
        # ``<import>``: not a test id, and the control plane's Python reader drops it too.
        ok = isinstance(nodeid, str) and nodeid and nodeid != IMPORT_CONTEXT
        return {"test": nodeid} if ok else None
    if value.get("kind") == "func":
        return _triple([value.get("path"), value.get("qualname"), value.get("line")])
    return None
