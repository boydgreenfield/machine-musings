"""Convert one extracted pytest-coverset shard bundle into a :class:`~coverset.lang.base.NeutralBundle`.

Inputs (see the plan's pytest-coverset "Bundle" section and Addendum B):

- ``coverage.sqlite`` (required): coverage.py schema v7, one context per test. Only the documented
  tables ``file``, ``context``, ``line_bits`` and ``arc`` are read, read-only and immutable. With
  branch coverage coverage.py records arcs (lines are their positive endpoints); with line coverage
  it records numbits.
- ``results.jsonl`` (required): one object per test; the last row for a nodeid wins.
- ``coverage.json`` (format 3): static facts and executed branch arcs.
- ``shapes.jsonl.gz``, ``collect.json``, ``durations.json``, ``invariant_links.jsonl``,
  ``meta.json``: optional.

Per-test lines are the raw physical lines the tracer reported; like coverage.py's own
``translate_lines`` they are mapped to the first line of the statement that contains them, so
they compare directly with ``coverage.json``'s statements (see :func:`first_lines`).

Every reader enforces :class:`~coverset.facts.untrusted.BundleLimits`; dropped records are counted
in ``NeutralBundle.meta['convert']`` instead of failing the shard, and anything malformed beyond
that raises :class:`~coverset.facts.untrusted.BundleError` (never another exception type).

File names are repository-relative. pytest-coverset writes every path that way from any working
directory and says so with ``meta.coverage_paths == "repository"``. Otherwise ``coverage.json`` and
``coverage.sqlite`` names are coverage.py's, relative to the directory pytest ran in, and
``meta.cwd`` (that directory relative to the repository root) is joined in front of each relative
name; a test's file taken from its nodeid gets ``meta.rootdir`` in front, since nodeids are
rootdir-relative. Values that are not normalised relative paths (bundles written before these keys
existed recorded absolute ``cwd`` and ``rootdir``) join nothing, so such bundles read as they always
did. Absolute names are dropped and counted (``bad_paths``) either way.

``meta.json``'s ``tracer`` is checked: per-test contexts need coverage.py's C tracer, so any other
named tracer (``PyTracer``, ``SysMonitor``) sets ``meta['convert']['tracer_mismatch'] = True`` and
``meta['convert']['tracer']`` to the (clipped) name; the shard is still ingested. A bundle without a
``tracer`` is not flagged.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import zlib
from collections import Counter, defaultdict
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args
from urllib.parse import quote

import numpy as np

from coverset.facts.untrusted import (
    BundleError,
    BundleLimits,
    CappedReader,
    StreamCapError,
    extract_bundle,
    iter_json_lines,
    open_regular,
    safe_rel_path,
)
from coverset.lang.base import (
    IMPORT_CONTEXT,
    CollectedItem,
    FuncSampling,
    FuncTriple,
    NeutralBundle,
    Outcome,
    ShapeObs,
    StaticFile,
    TestRow,
)

__all__ = [
    "BundleError",
    "BundleLimits",
    "extract_bundle",
    "first_lines",
    "is_shape_key",
    "numbits_to_lines",
    "read_python_bundle",
    "tracer_mismatch",
]

COVERAGE_SQLITE = "coverage.sqlite"
COVERAGE_JSON = "coverage.json"
SHAPES = "shapes.jsonl.gz"
RESULTS = "results.jsonl"
COLLECT = "collect.json"
DURATIONS = "durations.json"
INVARIANT_LINKS = "invariant_links.jsonl"
META = "meta.json"
BUNDLE_FILES = (COVERAGE_SQLITE, COVERAGE_JSON, SHAPES, RESULTS, COLLECT, DURATIONS, INVARIANT_LINKS, META)

_OUTCOMES: frozenset[str] = frozenset(get_args(Outcome.__value__))
_PHASES = frozenset({"setup", "call", "teardown"})
_COVERAGE_TABLES = ("file", "context", "line_bits", "arc")
_MAX_SCHEMA_OBJECTS = 256  # coverage.py's schema v7 has about a dozen tables and indexes
_MAX_NAME_CHARS = 512
_MAX_COUNT = 2**63 - 1
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_FETCH_ROWS = 65_536
_SHAPE_TYPE = re.compile(r"(?:NoneType|bool|int|float|str|bytes|list|tuple|dict|set|frozenset|obj:[^|\s]+)")
_SHAPE_FEATURE = re.compile(r"[^|\s]+")
C_TRACERS: frozenset[str] = frozenset({"CTracer", "ctrace"})
"""``meta.json`` ``tracer`` values that mean coverage.py's C tracer (the only one with dynamic contexts)."""
_MAX_TRACER_CHARS = 64


def tracer_mismatch(meta: object) -> str | None:
    """The tracer named by a bundle's ``meta.json`` when it is not the C tracer, else None.

    A C tracer that the plugin saw replaced mid-run (``meta.tracer_lost_at``, the context that was
    running when the trace function changed) also counts: the text is ``"<tracer> lost at
    <context>"``. The text is clipped and stripped to printable characters (the bundle is
    untrusted).
    """
    tracer = meta.get("tracer") if isinstance(meta, dict) else None
    if tracer is not None and not (isinstance(tracer, str) and tracer in C_TRACERS):
        text = "".join(ch for ch in str(tracer)[:_MAX_TRACER_CHARS] if ch.isprintable())
        return text or "an unnamed tracer"
    lost = meta.get("tracer_lost_at") if isinstance(meta, dict) else None
    if isinstance(lost, str) and lost:
        text = f"{tracer or 'CTracer'} lost at {lost}"
        return "".join(ch for ch in text if ch.isprintable())[:_MAX_TRACER_CHARS]
    return None


def numbits_to_lines(numbits: bytes) -> np.ndarray:
    """Decode a coverage.py numbits blob into the sorted line numbers it encodes (int64 array)."""
    bits = np.unpackbits(np.frombuffer(numbits, dtype=np.uint8), bitorder="little")
    return np.flatnonzero(bits)


def is_shape_key(value: object) -> bool:
    """Whether ``value`` is a canonical Addendum B shape key: a type token, then sorted features."""
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_NAME_CHARS or not value.isprintable():
        return False
    head, *features = value.split("|")
    return (
        _SHAPE_TYPE.fullmatch(head) is not None
        and all(_SHAPE_FEATURE.fullmatch(f) for f in features)
        and features == sorted(features)
    )


def first_lines(lines: np.ndarray, statements: Collection[int], excluded: Collection[int]) -> np.ndarray:
    """Map raw executed lines to the first line of their statement (sorted, unique).

    A raw line that is not itself a statement belongs to the statement that starts closest above
    it (a continuation line of a multi-line statement); lines inside an excluded statement keep
    their number. This reproduces coverage.py's ``translate_lines`` without re-parsing the source
    (checked against ``coverage.json`` on parenthesised and multi-line expressions, comprehensions,
    stacked decorators, try/else/finally, with, match, while-else, lambdas and excluded code).
    """
    return _Anchors.of(statements, excluded).map(lines)


@dataclass(frozen=True, slots=True)
class _Anchors:
    """The sorted first lines of a file's statements and excluded statements."""

    lines: np.ndarray
    is_statement: np.ndarray

    @classmethod
    def of(cls, statements: Collection[int], excluded: Collection[int]) -> _Anchors:
        stmts = np.asarray(sorted(statements), dtype=np.int64)
        anchors = np.union1d(stmts, np.asarray(sorted(excluded), dtype=np.int64))
        return cls(anchors, np.isin(anchors, stmts))

    def map(self, lines: np.ndarray) -> np.ndarray:
        if self.lines.size == 0 or lines.size == 0:
            return np.unique(lines)
        index = np.searchsorted(self.lines, lines, side="right") - 1
        inside = index >= 0
        hit = inside & self.is_statement[np.where(inside, index, 0)]
        mapped = lines.copy()
        mapped[hit] = self.lines[index[hit]]
        return np.unique(mapped)


@dataclass(slots=True)
class _Reader:
    """State shared by the per-file readers of one bundle."""

    root: Path
    limits: BundleLimits
    source_root: Path | None
    head_paths: frozenset[str] | None
    stats: Counter[str] = field(default_factory=Counter)
    present: dict[str, int] = field(default_factory=dict)
    digest: Any = field(default_factory=hashlib.sha256)
    paths: set[str] = field(default_factory=set)
    nodeids: set[str] = field(default_factory=set)
    shape_keys: set[str] = field(default_factory=set)
    measured_lengths: set[str] = field(default_factory=set)
    coverage_prefix: str = ""
    """Joined in front of relative ``coverage.json``/``coverage.sqlite`` names (``meta.cwd + "/"``)."""
    rootdir_prefix: str = ""
    """Joined in front of a test file taken from a (rootdir-relative) nodeid (``meta.rootdir + "/"``)."""

    # -- generic helpers -------------------------------------------------------------------

    def locate(self, meta: dict[str, Any]) -> None:
        """Set the name prefixes from ``meta`` (see the module docstring)."""
        if meta.get("coverage_paths") != "repository":
            self.coverage_prefix = _dir_prefix(meta.get("cwd"))
        self.rootdir_prefix = _dir_prefix(meta.get("rootdir"))

    def coverage_path(self, value: object) -> str | None:
        """A ``coverage.json``/``coverage.sqlite`` file name as a repository path (or None, counted)."""
        if self.coverage_prefix and isinstance(value, str) and safe_rel_path(value) is not None:
            value = self.coverage_prefix + value
        return self.rel_path(value)

    def test_file(self, raw: dict[str, Any], nodeid: str) -> str | None:
        """A result's or collected item's ``file``, else its nodeid's file part under the rootdir."""
        value = raw.get("file")
        if not value:
            value = nodeid.split("::", 1)[0]
            if self.rootdir_prefix and safe_rel_path(value) is not None:
                value = self.rootdir_prefix + value
        return self.rel_path(value)

    def path(self, name: str) -> Path | None:
        return self.root / name if name in self.present else None

    def nodeid(self, value: object) -> str | None:
        if not isinstance(value, str) or not value or value == IMPORT_CONTEXT:
            self.stats["bad_nodeids"] += 1
            return None
        if len(value) > self.limits.max_nodeid_chars:
            self.stats["long_nodeids"] += 1
            return None
        _remember(self.nodeids, value, self.limits, "max_tests", "nodeids")
        return value

    def rel_path(self, value: object) -> str | None:
        checked = safe_rel_path(value)
        if checked is None:
            self.stats["bad_paths"] += 1
            return None
        if self.head_paths is not None and checked not in self.head_paths:
            self.stats["paths_not_in_head"] += 1
            return None
        _remember(self.paths, checked, self.limits, "max_paths", "paths")
        return checked

    def shape_key(self, value: object) -> str | None:
        if not isinstance(value, str) or not is_shape_key(value):
            self.stats["shapes.bad_keys"] += 1
            return None
        _remember(self.shape_keys, value, self.limits, "max_shape_keys", "shape keys")
        return value

    def line(self, value: object) -> int | None:
        return _count(value, self.limits.max_line_number)

    def clip(self, value: object, limit: int, counter: str) -> str | None:
        if not isinstance(value, str):
            return None
        if len(value) > limit:
            self.stats[counter] += 1
            return value[: limit - 1] + "…"
        return value

    def load_json(self, name: str) -> Any:
        path = self.path(name)
        if path is None:
            return None
        if self.present[name] > self.limits.max_json_file_bytes:
            self.stats[f"{name}.oversized"] += 1
            return None
        with open_regular(path) as fh:
            raw = fh.read(self.limits.max_json_file_bytes + 1)
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            self.stats[f"{name}.unparseable"] += 1
            return None

    def json_lines(self, name: str) -> Iterator[dict[str, Any]]:
        path = self.path(name)
        if path is None:
            return
        with open_regular(path) as fh:
            yield from iter_json_lines(fh, self.limits, self.stats, name)

    # -- inventory -------------------------------------------------------------------------

    def inventory(self) -> None:
        """Note which of the known files exist, size-check them and hash them.

        Only the files that are read count against ``max_bundle_bytes``: anything else in the
        directory (the plugin's own ``bundle.tar.gz``, which packs these same files, its
        ``neutral/`` copy, an earlier report) is never opened, so it is not counted either. An
        extracted archive cannot hold more than the cap anyway (:func:`extract_bundle` bounds it).
        """
        root_st = os.lstat(self.root)
        if not stat.S_ISDIR(root_st.st_mode):
            raise BundleError("bundle path is not a directory")
        for name in BUNDLE_FILES:
            candidate = self.root / name
            try:
                st = os.lstat(candidate)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode):
                self.stats["refused_files"] += 1
                continue
            self.present[name] = st.st_size
        total = sum(self.present.values())
        if total > self.limits.max_bundle_bytes:
            raise self.limits.exceeded(
                f"bundle is {total} bytes (limit {self.limits.max_bundle_bytes})",
                cap="max_bundle_bytes",
                value=total,
            )
        self.stats["bundle_bytes"] = total
        for name, size in self.present.items():
            self.digest.update(f"{name}\0{size}\0".encode())
            with open_regular(self.root / name) as fh:
                while chunk := fh.read(1 << 20):
                    self.digest.update(chunk)
        for required in (COVERAGE_SQLITE, RESULTS):
            if required not in self.present:
                raise BundleError(f"bundle is missing {required}")

    # -- coverage.sqlite -------------------------------------------------------------------

    def coverage(
        self, static: dict[str, StaticFile], *, want_arcs: bool
    ) -> tuple[dict[str, dict[str, tuple[int, ...]]], dict[str, tuple[tuple[int, int], ...]]]:
        """Per-context executed lines (first lines) and, when ``want_arcs``, executed arcs per file."""
        path = self.root / COVERAGE_SQLITE
        uri = f"file:{quote(str(path))}?mode=ro&immutable=1"
        try:
            con = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise BundleError(f"cannot open coverage.sqlite: {exc}") from exc
        try:
            con.execute("PRAGMA trusted_schema=OFF")
            return _CoverageDb(self, con).read(static, want_arcs=want_arcs)
        except sqlite3.Error as exc:
            raise BundleError(f"unreadable coverage.sqlite: {exc}") from exc
        finally:
            con.close()

    # -- coverage.json ---------------------------------------------------------------------

    def static(self) -> tuple[dict[str, StaticFile], dict[str, tuple[tuple[int, int], ...]]] | None:
        """Static facts and executed branch arcs per file, or None without ``coverage.json``."""
        report = self.load_json(COVERAGE_JSON)
        if report is None:
            return None
        meta = report.get("meta") if isinstance(report, dict) else None
        files = report.get("files") if isinstance(report, dict) else None
        if not isinstance(meta, dict) or meta.get("format") != 3 or not isinstance(files, dict):
            self.stats["coverage.json.unsupported"] += 1
            return None
        static: dict[str, StaticFile] = {}
        run_arcs: dict[str, tuple[tuple[int, int], ...]] = {}
        for raw_path, entry in files.items():
            path = self.coverage_path(raw_path)
            if path is None:
                continue
            parsed = _parse_file_entry(entry, self.limits.max_line_number)
            if parsed is None:
                self.stats["coverage.json.bad_files"] += 1
                continue
            executed, missing, excluded, executed_arcs, missing_arcs = parsed
            statements = tuple(sorted(executed | missing))
            arcs_possible = tuple(sorted(executed_arcs | missing_arcs))
            seen = [*statements, *excluded, *(line for arc in arcs_possible for line in arc)]
            static[path] = StaticFile(
                path=path,
                n_lines=self._n_lines(path, max(seen, default=0)),
                statements=statements,
                excluded=tuple(sorted(excluded)),
                arcs_possible=arcs_possible,
            )
            run_arcs[path] = tuple(sorted(executed_arcs))
        return static, run_arcs

    def _n_lines(self, path: str, fallback: int) -> int:
        """Line count of ``path`` in the head checkout, else ``fallback``.

        The checkout holds the PR's files, so the path must resolve inside ``source_root`` (no
        symlink out of it) and be a regular file of at most 16 MiB. Paths measured this way are
        remembered: only their executed lines are bounded by ``n_lines`` (the fallback, the largest
        line ``coverage.json`` mentions, can be below a non-statement line such as a docstring).
        """
        if self.source_root is None:
            return fallback
        root = self.source_root.resolve()
        try:
            candidate = (root / path).resolve(strict=True)
            if not candidate.is_relative_to(root):
                return fallback
            with open_regular(candidate) as fh:
                data = fh.read(_MAX_SOURCE_BYTES + 1)
        except (OSError, BundleError):
            return fallback
        if len(data) > _MAX_SOURCE_BYTES:
            return fallback
        self.measured_lengths.add(path)
        # Lines as Python numbers them: ``\n``, ``\r\n`` and a lone ``\r`` each end one.
        return min(len(data.splitlines()), self.limits.max_line_number)

    # -- results / collect / durations -----------------------------------------------------

    def tests(self) -> tuple[TestRow, ...]:
        rows: dict[str, TestRow] = {}
        for raw in self.json_lines(RESULTS):
            if raw.get("kind", "test") != "test":
                continue
            row = self._test_row(raw)
            if row is None:
                self.stats["results.bad_rows"] += 1
            else:
                rows[row.nodeid] = row
        return tuple(rows.values())

    def _test_row(self, raw: dict[str, Any]) -> TestRow | None:
        nodeid = self.nodeid(raw.get("nodeid"))
        outcome = raw.get("outcome")
        if nodeid is None or outcome not in _OUTCOMES:
            return None
        file = self.test_file(raw, nodeid)
        duration = _number(raw.get("duration_s", raw.get("duration")))
        phase = raw.get("failed_phase")
        if file is None:
            return None
        return TestRow(
            nodeid=nodeid,
            file=file,
            outcome=outcome,
            duration_s=max(duration or 0.0, 0.0),
            failed_phase=phase if phase in _PHASES else None,
            fail_sig=self.clip(raw.get("fail_sig"), 64, "results.long_fail_sigs"),
            longrepr=self.clip(raw.get("longrepr"), self.limits.max_longrepr_chars, "results.long_longreprs"),
            start=_number(raw.get("start")),
            stop=_number(raw.get("stop")),
        )

    def collected(self) -> tuple[CollectedItem, ...]:
        data = self.load_json(COLLECT)
        items = data.get("items") if isinstance(data, dict) else data
        links: dict[str, set[str]] = defaultdict(set)
        for raw in self.json_lines(INVARIANT_LINKS):
            # pytest-coverset writes {"test_id": nodeid, "invariant_id": id}; "nodeid" is accepted too.
            nodeid = self.nodeid(raw.get("test_id", raw.get("nodeid")))
            inv = raw.get("invariant_id", raw.get("invariant"))
            if nodeid is not None and isinstance(inv, str) and 0 < len(inv) <= _MAX_NAME_CHARS:
                links[nodeid].add(inv)
            else:
                self.stats["invariant_links.bad_rows"] += 1
        result: list[CollectedItem] = []
        for raw in items if isinstance(items, list) else []:
            item = self._collected_item(raw, links) if isinstance(raw, dict) else None
            if item is None:
                self.stats["collect.bad_items"] += 1
            else:
                result.append(item)
        return tuple(result)

    def _collected_item(self, raw: dict[str, Any], links: dict[str, set[str]]) -> CollectedItem | None:
        nodeid = self.nodeid(raw.get("nodeid"))
        if nodeid is None:
            return None
        file = self.test_file(raw, nodeid)
        if file is None:
            return None
        invariants = {i for i in _names(raw.get("invariant_ids")) if i}
        return CollectedItem(
            nodeid=nodeid,
            file=file,
            line=self.line(raw.get("line")),
            markers=tuple(_names(raw.get("markers"))),
            invariant_ids=tuple(sorted(invariants | links.get(nodeid, set()))),
            hypothesis=raw.get("hypothesis") is True,
        )

    def durations(self, tests: tuple[TestRow, ...]) -> dict[str, float]:
        data = self.load_json(DURATIONS)
        if not isinstance(data, dict):
            return {row.nodeid: row.duration_s for row in tests}
        result: dict[str, float] = {}
        for raw_nodeid, raw_value in data.items():
            nodeid = self.nodeid(raw_nodeid)
            value = _number(raw_value)
            if nodeid is not None and value is not None and value >= 0:
                result[nodeid] = value
        return result

    def meta(self) -> dict[str, Any]:
        data = self.load_json(META)
        return dict(data) if isinstance(data, dict) else {}

    # -- shapes.jsonl.gz -------------------------------------------------------------------

    def shapes(self) -> tuple[tuple[ShapeObs, ...], tuple[FuncSampling, ...]]:
        path = self.path(SHAPES)
        if path is None:
            return (), ()
        cap = self.limits.gzip_cap(self.present[SHAPES])
        obs: list[ShapeObs] = []
        sampling: list[FuncSampling] = []
        try:
            with open_regular(path) as raw, gzip.GzipFile(fileobj=raw, mode="rb") as gz:
                rows = iter_json_lines(CappedReader(gz, cap, SHAPES), self.limits, self.stats, "shapes")
                header = _ShapesHeader.parse(next(rows, None), self)
                if header is None:
                    self.stats["shapes.bad_header"] += 1
                    return (), ()
                for row in rows:
                    kind = row.get("kind")
                    parsed = (
                        header.obs(row, self)
                        if kind == "obs"
                        else header.sampling(row)
                        if kind == "func_sampling"
                        else None
                    )
                    if parsed is None:
                        self.stats["shapes.bad_rows"] += 1
                    elif isinstance(parsed, ShapeObs):
                        obs.append(parsed)
                    else:
                        sampling.append(parsed)
        except StreamCapError:
            self.stats["shapes.capped"] += 1
        except (OSError, EOFError, zlib.error):
            self.stats["shapes.corrupt"] += 1
        return tuple(obs), tuple(sampling)


def _names(value: object) -> list[str]:
    """The strings of a JSON list that are short enough to be names (markers, invariant ids)."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and len(v) <= _MAX_NAME_CHARS]


def _dir_prefix(value: object) -> str:
    """``value/`` for a normalised relative directory, empty for ``.`` and anything else."""
    checked = safe_rel_path(value)
    return f"{checked}/" if checked is not None else ""


def _remember(seen: set[str], value: str, limits: BundleLimits, cap: str, what: str) -> None:
    """Add ``value`` to ``seen``; more distinct values than the cap ``cap`` reject the bundle."""
    if value not in seen:
        seen.add(value)
        limit = getattr(limits, cap)
        if len(seen) > limit:
            raise limits.exceeded(f"bundle names more than {limit} distinct {what}", cap=cap, value=len(seen))


class _CoverageDb:
    """Bounded decoding of one ``coverage.sqlite`` (arcs and numbits) into per-test first lines.

    Rows are streamed: each (context, file) pair is decoded, mapped to first lines and stored as
    soon as it is read (numbits hold one row per pair; arcs are grouped per pair by SQLite, along
    the table's own ``(file_id, context_id, ...)`` index), so no second copy of the executed
    lines is ever held. Every stored line is one shared int object (:meth:`_tuple`), 8 bytes per
    cell.
    """

    def __init__(self, reader: _Reader, con: sqlite3.Connection) -> None:
        self.reader = reader
        self.con = con
        self.limits = reader.limits
        self.cells = 0
        self._anchors: dict[str, _Anchors] = {}
        self._static: dict[str, StaticFile] = {}
        self._pool: list[int] = []
        self.coverage: dict[str, dict[str, tuple[int, ...]]] = defaultdict(dict)

    def read(
        self, static: dict[str, StaticFile], *, want_arcs: bool
    ) -> tuple[dict[str, dict[str, tuple[int, ...]]], dict[str, tuple[tuple[int, int], ...]]]:
        kinds = self._schema()
        self._static = static
        files: dict[int, str] = {}
        for file_id, raw_path in self.con.execute("SELECT id, path FROM file WHERE typeof(id) = 'integer'"):
            checked = self.reader.coverage_path(raw_path)
            if checked is not None:
                files[file_id] = checked
        contexts: dict[int, str] = {}
        for context_id, raw_context in self.con.execute(
            "SELECT id, context FROM context WHERE typeof(id) = 'integer'"
        ):
            if raw_context == "":
                contexts[context_id] = IMPORT_CONTEXT
            elif (nodeid := self.reader.nodeid(raw_context)) is not None:
                contexts[context_id] = nodeid

        if "line_bits" in kinds:
            self._numbits(files, contexts)
        arcs: dict[str, tuple[tuple[int, int], ...]] = {}
        if "arc" in kinds:
            self._arc_lines(files, contexts)
            if want_arcs:
                arcs = self._run_arcs(files)
        return dict(self.coverage), arcs

    def _schema(self) -> set[str]:
        """The documented tables present, refusing anything a writer could use to run code on read.

        The four tables must be plain tables (``CREATE TABLE``), never views or virtual tables (a
        virtual table's module runs on every SELECT, and ``trusted_schema=OFF`` does not stop that);
        a schema holding any trigger or any virtual table at all is refused outright.
        """
        (objects,) = self.con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if objects > _MAX_SCHEMA_OBJECTS:
            raise BundleError(f"coverage.sqlite has more than {_MAX_SCHEMA_OBJECTS} schema objects")
        present: set[str] = set()
        for name, kind, sql in self.con.execute("SELECT name, type, sql FROM sqlite_master"):
            text = sql.lstrip().upper() if isinstance(sql, str) else ""
            if kind == "trigger" or text.startswith("CREATE VIRTUAL TABLE"):
                raise BundleError("coverage.sqlite has triggers or virtual tables")
            if name in _COVERAGE_TABLES:
                if kind != "table" or not text.startswith("CREATE TABLE"):
                    raise BundleError("coverage.sqlite does not have the schema-v7 tables")
                present.add(name)
        if not {"file", "context"} <= present:
            raise BundleError("coverage.sqlite does not have the schema-v7 tables")
        return present

    def _add_cells(self, n: int) -> None:
        self.cells += n
        if self.cells > self.limits.max_coverage_cells:
            raise self.limits.exceeded(
                f"coverage.sqlite holds more than {self.limits.max_coverage_cells} cells",
                cap="max_coverage_cells",
                value=self.cells,
            )

    def _arc_filter(self) -> str:
        limit = self.limits.max_line_number
        return (
            "typeof(file_id) = 'integer' AND typeof(context_id) = 'integer' "
            f"AND typeof(fromno) = 'integer' AND fromno BETWEEN -{limit} AND {limit} "
            f"AND typeof(tono) = 'integer' AND tono BETWEEN -{limit} AND {limit}"
        )

    def _numbits(self, files: dict[int, str], contexts: dict[int, str]) -> None:
        max_blob = self.limits.max_line_number // 8 + 1
        for file_id, context_id, numbits in self.con.execute(
            "SELECT file_id, context_id, numbits FROM line_bits"
        ):
            if file_id not in files or context_id not in contexts:
                continue
            if not isinstance(numbits, bytes) or len(numbits) > max_blob:
                self.reader.stats["coverage.bad_numbits"] += 1
                continue
            lines = numbits_to_lines(numbits)
            lines = lines[lines > 0]
            self._add_cells(lines.size)
            self._keep(contexts[context_id], files[file_id], lines)

    def _arc_lines(self, files: dict[int, str], contexts: dict[int, str]) -> None:
        """Executed lines from arcs (their positive endpoints), one SQL row per (file, context).

        Grouping in SQLite (``group_concat``, along the arc table's unique index) avoids the
        sort of a ``UNION ... ORDER BY`` over every endpoint and one Python tuple per arc: on a
        15M-arc bundle this read takes a few seconds instead of half a minute.
        """
        valid = self._arc_filter()
        (total,) = self.con.execute("SELECT count(*) FROM arc").fetchone()
        if total > 2 * self.limits.max_coverage_cells:
            raise self.limits.exceeded(
                f"coverage.sqlite holds {total} arcs, over twice the cell limit",
                cap="max_coverage_cells",
                value=total,
            )
        kept = 0
        for context_id, file_id, count, froms, tos in self.con.execute(
            "SELECT context_id, file_id, count(*), group_concat(fromno), group_concat(tono) "
            f"FROM arc WHERE {valid} GROUP BY file_id, context_id"
        ):
            kept += count
            if file_id not in files or context_id not in contexts:
                continue
            ends = np.concatenate((_int_list(froms), _int_list(tos)))
            lines = np.unique(ends[ends > 0])
            self._add_cells(lines.size)
            self._keep(contexts[context_id], files[file_id], lines)
        if total > kept:
            self.reader.stats["coverage.bad_arcs"] += total - kept

    def _run_arcs(self, files: dict[int, str]) -> dict[str, tuple[tuple[int, int], ...]]:
        """Executed arcs per file (union over contexts), used only without ``coverage.json``."""
        arcs: dict[str, set[tuple[int, int]]] = defaultdict(set)
        cursor = self.con.execute(
            f"SELECT DISTINCT file_id, fromno, tono FROM arc WHERE {self._arc_filter()} AND fromno > 0"
        )
        count = 0
        while chunk := cursor.fetchmany(_FETCH_ROWS):
            count += len(chunk)
            if count > self.limits.max_coverage_cells:
                raise self.limits.exceeded(
                    f"coverage.sqlite holds more than {self.limits.max_coverage_cells} arcs",
                    cap="max_coverage_cells",
                    value=count,
                )
            for file_id, from_line, to_line in chunk:
                if file_id in files:
                    arcs[files[file_id]].add((from_line, to_line))
        return {path: tuple(sorted(found)) for path, found in arcs.items()}

    def _keep(self, nodeid: str, path: str, lines: np.ndarray) -> None:
        """Store the first lines of ``lines`` (raw executed lines of one test and file)."""
        lines = lines[(lines > 0) & (lines <= self.limits.max_line_number)]
        facts = self._static.get(path)
        if facts is not None:
            if path not in self._anchors:
                self._anchors[path] = _Anchors.of(facts.statements, facts.excluded)
            lines = self._anchors[path].map(lines)
            beyond = lines > facts.n_lines
            if path in self.reader.measured_lengths and beyond.any():
                self.reader.stats["coverage.lines_beyond_file"] += int(beyond.sum())
                lines = lines[~beyond]
        else:
            lines = np.unique(lines)
        if not lines.size:
            return
        by_path = self.coverage[nodeid]
        earlier = by_path.get(path)
        if earlier is not None:  # a pair in both tables, or twice in a table without its unique index
            lines = np.union1d(np.asarray(earlier, dtype=np.int64), lines)
        by_path[path] = self._tuple(lines)

    def _tuple(self, lines: np.ndarray) -> tuple[int, ...]:
        """``lines`` (sorted) as a tuple of shared int objects: one per distinct line number in the
        whole bundle, so a cell costs the tuple's 8-byte pointer and not a 28-byte int of its own."""
        pool = self._pool
        top = int(lines[-1])
        if top >= len(pool):
            pool.extend(range(len(pool), top + 1))
        return tuple(map(pool.__getitem__, lines.tolist()))


def _int_list(text: object) -> np.ndarray:
    """A ``group_concat`` of integers (``"3,4,-1"``) as an int64 array."""
    if isinstance(text, int):
        return np.asarray([text], dtype=np.int64)
    if not isinstance(text, str) or not text:
        return np.empty(0, dtype=np.int64)
    return np.fromstring(text, dtype=np.int64, sep=",")


@dataclass(frozen=True, slots=True)
class _ShapesHeader:
    """Decoded header dictionaries of ``shapes.jsonl.gz``; invalid entries are None."""

    funcs: tuple[FuncTriple | None, ...]
    shapes: tuple[str | None, ...]
    tests: tuple[str | None, ...]
    callers: tuple[FuncTriple | str | None, ...]

    @classmethod
    def parse(cls, row: dict[str, Any] | None, reader: _Reader) -> _ShapesHeader | None:
        if row is None or row.get("kind") != "header" or row.get("version") != 1:
            return None
        lists = [row.get(key) for key in ("funcs", "shapes", "tests", "callers")]
        if not all(isinstance(value, list) for value in lists):
            return None
        funcs, shapes, tests, callers = lists
        return cls(
            funcs=tuple(_func_triple(f, reader) for f in funcs),
            shapes=tuple(reader.shape_key(s) for s in shapes),
            tests=tuple(reader.nodeid(t) for t in tests),
            callers=tuple(_caller(c, reader) for c in callers),
        )

    def _tests(self, indexes: object) -> tuple[str, ...] | None:
        if not isinstance(indexes, list):
            return None
        found = (_lookup(self.tests, i) for i in indexes)
        return tuple(dict.fromkeys(t for t in found if t is not None))

    def obs(self, row: dict[str, Any], reader: _Reader) -> ShapeObs | None:
        func = _lookup(self.funcs, row.get("f"))
        shape = _lookup(self.shapes, row.get("s"))
        caller = _lookup(self.callers, row.get("c"))
        param = row.get("p")
        samples = _count(row.get("n"))
        tests = self._tests(row.get("t"))
        if None in (func, shape, caller, samples, tests) or not isinstance(param, str):
            return None
        if not 0 < len(param) <= _MAX_NAME_CHARS:
            return None
        example_test = row.get("ext")
        return ShapeObs(
            func=func,
            param=param,
            shape_key=shape,
            caller=caller,
            samples=samples,
            tests=tests,
            truncated=row.get("trunc") is True,
            example=reader.clip(row.get("ex"), reader.limits.max_repr_chars, "shapes.truncated_reprs"),
            example_test=_lookup(self.tests, example_test) if example_test is not None else None,
        )

    def sampling(self, row: dict[str, Any]) -> FuncSampling | None:
        func = _lookup(self.funcs, row.get("f"))
        calls = _count(row.get("calls"))
        tests = self._tests(row.get("tests"))
        if func is None or calls is None or tests is None:
            return None
        truncated = row.get("truncated") is True
        cap = row.get("cap")
        return FuncSampling(
            func=func,
            calls=calls,
            tests=tests,
            truncated=truncated,
            cap=cap if truncated and cap in ("sample", "full") else None,
        )


def _lookup[T](table: tuple[T | None, ...], index: object) -> T | None:
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(table):
        return table[index]
    return None


def _count(value: object, maximum: int = _MAX_COUNT) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum:
        return value
    return None


def _number(value: object) -> float | None:
    """A finite float from a JSON number, else None (huge integers and non-numbers included)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _func_triple(value: object, reader: _Reader) -> FuncTriple | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    path, qualname, line = value
    checked = reader.rel_path(path)
    if checked is None or not isinstance(qualname, str) or not 0 < len(qualname) <= _MAX_NAME_CHARS:
        return None
    checked_line = reader.line(line)
    if checked_line is None:
        return None
    return (checked, qualname, checked_line)


def _caller(value: object, reader: _Reader) -> FuncTriple | str | None:
    if not isinstance(value, dict):
        return None
    if value.get("kind") == "test":
        if value.get("nodeid") == IMPORT_CONTEXT:
            return None  # a decorator applied at import time: expected, not a bad nodeid
        return reader.nodeid(value.get("nodeid"))
    if value.get("kind") == "func":
        return _func_triple([value.get("path"), value.get("qualname"), value.get("line")], reader)
    return None


def _int_set(value: object, low: int, high: int) -> set[int] | None:
    if not isinstance(value, list):
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) and low <= v <= high for v in value):
        return None
    return set(value)


def _arc_set(value: object, limit: int) -> set[tuple[int, int]] | None:
    if not isinstance(value, list):
        return None
    arcs: set[tuple[int, int]] = set()
    for arc in value:
        pair = _int_set(arc, -limit, limit) if isinstance(arc, list) and len(arc) == 2 else None
        if pair is None:
            return None
        arcs.add((arc[0], arc[1]))
    return arcs


def _parse_file_entry(
    entry: object, limit: int
) -> tuple[set[int], set[int], set[int], set[tuple[int, int]], set[tuple[int, int]]] | None:
    if not isinstance(entry, dict):
        return None
    executed = _int_set(entry.get("executed_lines"), 1, limit)
    missing = _int_set(entry.get("missing_lines"), 1, limit)
    excluded = _int_set(entry.get("excluded_lines", []), 1, limit)
    executed_arcs = _arc_set(entry.get("executed_branches", []), limit)
    missing_arcs = _arc_set(entry.get("missing_branches", []), limit)
    if None in (executed, missing, excluded, executed_arcs, missing_arcs):
        return None
    return executed, missing, excluded, executed_arcs, missing_arcs


def read_python_bundle(
    bundle_dir: Path,
    *,
    limits: BundleLimits | None = None,
    source_root: Path | None = None,
    head_paths: Collection[str] | None = None,
) -> NeutralBundle:
    """Read an extracted pytest-coverset bundle directory into a :class:`NeutralBundle`.

    ``source_root`` (optional) is a checkout of the run's head commit; when given, each file's
    ``n_lines`` is read from it, otherwise it is the largest line number the bundle mentions.
    ``head_paths`` (optional) is the set of files in the head tree: any other path in the bundle
    is dropped and counted (``paths_not_in_head``), as the untrusted-bundle rules require.
    Raises :class:`BundleError` when a required file is missing, a hard limit is exceeded or the
    bundle is malformed beyond repair; softer problems drop the offending record and are counted
    in ``meta['convert']['dropped']``.
    """
    reader = _Reader(
        root=Path(bundle_dir),
        limits=limits or BundleLimits(),
        source_root=source_root,
        head_paths=frozenset(head_paths) if head_paths is not None else None,
    )
    try:
        return _read(reader)
    except BundleError:
        raise
    except (sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError, OSError, zlib.error) as exc:
        raise BundleError(f"malformed bundle ({type(exc).__name__}): {exc}") from exc


def _read(reader: _Reader) -> NeutralBundle:
    reader.inventory()
    meta = reader.meta()
    reader.locate(meta)
    static_result = reader.static()
    static, run_arcs = static_result if static_result is not None else ({}, {})
    coverage, sqlite_arcs = reader.coverage(static, want_arcs=static_result is None)
    if static_result is None:
        run_arcs = sqlite_arcs
    tests = reader.tests()
    shapes, sampling = reader.shapes()
    bundle = NeutralBundle(
        meta=meta,
        collected=reader.collected(),
        tests=tests,
        coverage=coverage,
        static=static,
        run_arcs=run_arcs,
        shapes=shapes,
        sampling=sampling,
        durations=reader.durations(tests),
    )
    meta["convert"] = {
        "bundle_sha256": reader.digest.hexdigest(),
        "files": sorted(reader.present),
        "missing_files": sorted(set(BUNDLE_FILES) - reader.present.keys()),
        "dropped": {key: value for key, value in sorted(reader.stats.items()) if key != "bundle_bytes"},
        "bundle_bytes": reader.stats["bundle_bytes"],
        "contexts": len(coverage),
        "head_checked": reader.head_paths is not None,
    }
    if (tracer := tracer_mismatch(meta)) is not None:
        meta["convert"]["tracer_mismatch"] = True
        meta["convert"]["tracer"] = tracer
    return bundle
