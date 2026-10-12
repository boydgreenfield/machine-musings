"""Read, validate and write neutral bundles on disk (``docs/neutral-bundle.md``).

A bundle is a directory, or a ``tar.gz`` of one with flat members, holding ``manifest.json`` and
the ``.jsonl`` (or ``.jsonl.gz``) files. Every byte of it is untrusted: a pull request's own code
ran next to the reporter that wrote it. The reader therefore

* refuses (raises :class:`BundleRejected`) a bundle over the size limits, a gzip stream that
  expands beyond its ratio cap, an archive with links, devices, nested paths or too many members,
  an unknown ``format`` or ``version``, a missing required file, a file with more lines than its
  row cap, a read that outlasts the wall-clock budget, or more distinct tests, paths, shape keys,
  functions or coverage cells than :class:`Limits` allows;
* drops, and counts in ``meta["convert"]["dropped"]``, every row that breaks the schema
  (:mod:`coverset_model.schema`) or the rules the schema cannot express: unsafe paths, a test id
  used twice, a reserved id, lines that are not statements of their file, a function whose key or
  line range does not add up, a shape key outside the grammar. Overlong failure messages and
  examples are clipped, not dropped; unknown fields are ignored.

:func:`validate` runs the same checks and returns every :class:`Problem` instead (file, row, field
and message), never raising. :func:`read_bundle` returns a
:class:`~coverset_model.neutral.NeutralBundle`; :func:`write_bundle` writes one, and
:func:`conform` says what the on-disk form keeps of an in-memory bundle, so
``read_bundle(write_bundle(b)) == conform(b)`` up to the reader's own ``meta`` keys.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import time
import zlib
from collections import Counter
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, Any, Final

from coverset_model import schema as schemas
from coverset_model import shape_keys
from coverset_model.neutral import (
    IMPORT_CONTEXT,
    CollectedItem,
    FrozenMap,
    FuncSampling,
    FunctionRegion,
    FuncTriple,
    NeutralBundle,
    ShapeObs,
    StaticFile,
    TestRow,
    func_key,
)
from coverset_model.types import DescriptorError, TypeDescriptor, from_json, to_json

__all__ = [
    "FORMAT",
    "OPTIONAL_FILES",
    "READER_META_KEYS",
    "REQUIRED_FILES",
    "VERSION",
    "BundleInfo",
    "BundleRejected",
    "Limits",
    "Problem",
    "conform",
    "inspect",
    "is_neutral",
    "pack",
    "read_bundle",
    "read_manifest",
    "safe_rel_path",
    "validate",
    "write_bundle",
]

FORMAT: Final = schemas.FORMAT
VERSION: Final = schemas.VERSION
MANIFEST: Final = "manifest.json"
REQUIRED_FILES: Final = ("tests", "files", "coverage")
OPTIONAL_FILES: Final = ("arcs", "functions", "shapes", "links")
STEMS: Final = (*REQUIRED_FILES, *OPTIONAL_FILES)
READER_META_KEYS: Final = frozenset({"bundle", "convert"})
"""Keys of ``NeutralBundle.meta`` the reader fills in itself (never written to ``manifest.meta``)."""
LANGUAGES_WITH_SOURCE_INDEX: Final = frozenset({"python"})
"""Languages whose adapter derives function regions from the sources (``functions.jsonl`` optional)."""

MIB: Final = 1024 * 1024
_DRIVE: Final = re.compile(r"^[A-Za-z]:")
_COPY_CHUNK: Final = 1 * MIB
_PHASES: Final = frozenset({"setup", "call", "teardown"})


@dataclass(frozen=True, slots=True)
class Limits:
    """Caps applied to an untrusted bundle (``docs/security.md`` section 7).

    The names and defaults match ``coverset.facts.untrusted.BundleLimits`` where both have a field,
    so a caller can pass the same numbers to the Python and the neutral reader.
    ``gzip_cap`` bounds every gzip stream (the archive and each ``.jsonl.gz``) at ``gzip_ratio``
    times its compressed size, but never below ``gzip_floor_bytes`` nor above ``gzip_max_bytes``.

    Work is bounded as well as bytes: a file with more lines than :meth:`rows_cap` allows (blank,
    oversized and malformed lines count), or a read that takes longer than ``max_seconds`` of
    wall-clock time, refuses the bundle. The defaults and the sizes they are measured against
    are in ``docs/security.md`` (B14) and the package README.
    """

    max_bundle_bytes: int = 200 * MIB
    gzip_ratio: int = 10
    gzip_floor_bytes: int = 16 * MIB
    gzip_max_bytes: int = 500 * MIB
    max_json_line_bytes: int = 1 * MIB
    max_manifest_bytes: int = 1 * MIB
    max_nodeid_chars: int = 512
    max_path_chars: int = 512
    max_repr_chars: int = 200
    max_longrepr_chars: int = 8 * 1024
    max_members: int = 256
    max_line_number: int = 1_000_000
    max_coverage_cells: int = 4_000_000
    """Distinct (test, file, line) cells kept from ``coverage.jsonl`` (about 8 bytes each once read)."""
    max_tests: int = 50_000
    max_paths: int = 50_000
    max_shape_keys: int = 20_000
    max_functions: int = 200_000
    max_rows: int = 200_000
    """Lines per file (every file but ``coverage.jsonl``); more refuses the bundle."""
    max_coverage_rows: int = 2_000_000
    """Lines of ``coverage.jsonl`` (one per (test, file) pair); more refuses the bundle."""
    max_seconds: float = 120.0
    """Wall-clock budget of one :func:`read_bundle` (or :func:`validate`); past it the bundle is refused."""
    max_problems: int = 1000
    """:func:`validate` lists at most this many problems, then one line counting the rest."""

    def rows_cap(self, stem: str) -> int:
        """How many lines the file ``stem`` (``coverage``, ``tests``, ...) may have."""
        return self.max_coverage_rows if stem == "coverage" else self.max_rows

    def gzip_cap(self, compressed_bytes: int) -> int:
        """Maximum decompressed bytes allowed for a gzip stream of ``compressed_bytes``."""
        ratio_cap = max(self.gzip_floor_bytes, self.gzip_ratio * compressed_bytes)
        return min(self.gzip_max_bytes, ratio_cap)

    @classmethod
    def from_object(cls, other: object) -> Limits:
        """Limits with every same-named field of ``other`` (e.g. a ``BundleLimits``) copied over."""
        names = cls.__dataclass_fields__
        return cls(**{name: getattr(other, name) for name in names if hasattr(other, name)})


@dataclass(frozen=True, slots=True)
class Problem:
    """One thing wrong with a bundle: where (file, 1-based row, field) and what."""

    file: str
    row: int | None
    field: str | None
    message: str
    code: str = "invalid"
    """A short machine-readable kind (``schema``, ``path``, ``not_statement``, ...)."""
    fatal: bool = False
    """The reader refuses the whole bundle for this problem."""

    def __str__(self) -> str:
        where = self.file + (f":{self.row}" if self.row is not None else "")
        return f"{where}: {self.field + ': ' if self.field else ''}{self.message}"


class BundleRejected(ValueError):
    """The bundle breaks a hard limit or is unreadable; nothing of it may be ingested."""

    def __init__(self, problem: Problem) -> None:
        super().__init__(str(problem))
        self.problem = problem


@dataclass(slots=True)
class BundleInfo:
    """What :func:`inspect` found: the manifest, row counts per file, problems and drops."""

    manifest: dict[str, Any] | None
    files: dict[str, str]
    """File stem → the member name it was read from (``tests`` → ``tests.jsonl.gz``)."""
    rows: dict[str, int]
    """Rows read per file stem (before any row was dropped)."""
    problems: list[Problem]
    dropped: dict[str, int]
    """``<stem>.<code>`` → how many rows (or values) the reader would drop or clip."""
    bundle_bytes: int = 0

    @property
    def language(self) -> str | None:
        return self.manifest.get("language") if isinstance(self.manifest, dict) else None


def safe_rel_path(value: object, max_chars: int = 512) -> str | None:
    """``value`` if it is a normalised, relative POSIX path of at most ``max_chars``, else None.

    Rejected: non-strings, empty strings, NUL, backslashes, other control characters, absolute
    paths, drive letters, and empty, ``.`` or ``..`` segments (``a//b``, ``./a`` and ``a/../b``
    are refused, not rewritten).
    """
    if not isinstance(value, str) or not 0 < len(value) <= max_chars or "\\" in value:
        return None
    if not value.isprintable() or value.startswith("/") or _DRIVE.match(value):
        return None
    if any(part in ("", ".", "..") for part in value.split("/")):
        return None
    return value


# --- sources -------------------------------------------------------------------------------------


class _Source:
    """The members of a bundle: a directory or an in-memory archive."""

    def __init__(self, members: dict[str, int], data: dict[str, bytes] | None, root: Path | None) -> None:
        self.members = members
        """Known member name → size on disk (compressed size for ``.gz``)."""
        self._data = data
        self._root = root

    def open(self, name: str) -> IO[bytes]:
        if self._data is not None:
            return io.BytesIO(self._data[name])
        assert self._root is not None
        return _open_regular(self._root / name)

    def size(self, name: str, fh: IO[bytes]) -> int:
        """Bytes of the opened member ``name`` (``fstat`` for a file on disk)."""
        if self._data is not None:
            return len(self._data[name])
        return os.fstat(fh.fileno()).st_size

    def release(self, name: str) -> None:
        """Forget an archive member's bytes once its file has been read."""
        if self._data is not None:
            self._data.pop(name, None)


def _known_names() -> list[str]:
    return [MANIFEST, *(f"{stem}{ext}" for stem in STEMS for ext in (".jsonl", ".jsonl.gz"))]


def _open_regular(path: Path) -> IO[bytes]:
    """Open ``path`` without following a symlink; refuse anything but a regular file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path.name} is not a regular file")
    os.set_blocking(fd, True)
    return os.fdopen(fd, "rb")


class _CappedReader:
    """A read-only stream that raises :class:`_Capped` past ``cap`` bytes."""

    def __init__(self, inner: IO[bytes], cap: int) -> None:
        self._inner = inner
        self._cap = cap
        self.consumed = 0

    def _account(self, data: bytes) -> bytes:
        self.consumed += len(data)
        if self.consumed > self._cap:
            raise _Capped(self._cap)
        return data

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._cap - self.consumed + 1
        return self._account(self._inner.read(size))

    def readline(self, size: int = -1) -> bytes:
        return self._account(self._inner.readline(size))


class _HashingReader:
    """Reads at most ``size`` bytes of ``inner``, feeding every byte read to ``digest``."""

    def __init__(self, inner: IO[bytes], digest: Any, size: int) -> None:
        self._inner = inner
        self._digest = digest
        self._left = size

    def _take(self, data: bytes) -> bytes:
        self._left -= len(data)
        self._digest.update(data)
        return data

    def read(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        size = self._left if size is None or size < 0 else min(size, self._left)
        return self._take(self._inner.read(size))

    def readline(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        size = self._left if size is None or size < 0 else min(size, self._left)
        return self._take(self._inner.readline(size))

    def drain(self) -> None:
        """Hash whatever was not read (bytes after a gzip stream's end)."""
        while self.read(_COPY_CHUNK):
            pass


class _Capped(Exception):
    def __init__(self, cap: int) -> None:
        super().__init__(cap)
        self.cap = cap


# --- the scan --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Scan:
    """One pass over a bundle: problems, counts and (always) the parsed data."""

    limits: Limits
    collect: bool
    """Keep every problem (``validate``) rather than only counting them (``read_bundle``)."""
    allowed_paths: frozenset[str] | None = None
    problems: list[Problem] = field(default_factory=list)
    overflow: int = 0
    stats: Counter[str] = field(default_factory=Counter)
    rows: dict[str, int] = field(default_factory=dict)
    files_read: dict[str, str] = field(default_factory=dict)
    digest: Any = field(default_factory=hashlib.sha256)
    bundle_bytes: int = 0
    manifest: dict[str, Any] | None = None
    # parsed data
    static: dict[str, StaticFile] = field(default_factory=dict)
    statement_sets: dict[str, frozenset[int]] = field(default_factory=dict)
    collected: dict[str, CollectedItem] = field(default_factory=dict)
    tests: dict[str, TestRow] = field(default_factory=dict)
    durations: dict[str, float] = field(default_factory=dict)
    links: dict[str, set[str]] = field(default_factory=dict)
    coverage: dict[str, dict[str, tuple[int, ...]]] = field(default_factory=dict)
    run_arcs: dict[str, set[tuple[int, int]]] = field(default_factory=dict)
    functions: dict[tuple[str, str, int], FunctionRegion] = field(default_factory=dict)
    types: dict[FuncTriple, FrozenMap[str, TypeDescriptor | None]] = field(default_factory=dict)
    shapes: list[ShapeObs] = field(default_factory=list)
    sampling: list[FuncSampling] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    seen_paths: set[str] = field(default_factory=set)
    seen_keys: set[str] = field(default_factory=set)
    cells: int = 0
    ints: dict[int, int] = field(default_factory=dict)
    """One int object per distinct line number, shared by every coverage tuple."""
    deadline: float = 0.0

    def __post_init__(self) -> None:
        self.deadline = time.monotonic() + self.limits.max_seconds

    def check_clock(self, file: str) -> None:
        if time.monotonic() > self.deadline:
            self.report(
                file,
                None,
                None,
                f"reading took longer than {self.limits.max_seconds:g} s (the wall-clock budget)",
                "limit",
                fatal=True,
            )

    # -- reporting ----------------------------------------------------------------------------

    def report(
        self,
        file: str,
        row: int | None,
        fld: str | None,
        message: str,
        code: str,
        *,
        fatal: bool = False,
        count: int = 1,
    ) -> None:
        problem = Problem(file, row, fld, message, code, fatal)
        if fatal:
            self.problems.append(problem)
            raise BundleRejected(problem)
        self.stats[f"{_stem(file)}.{code}"] += count
        if not self.collect:
            return
        if len(self.problems) < self.limits.max_problems:
            self.problems.append(problem)
        else:
            self.overflow += 1

    # -- shared checks ---------------------------------------------------------------------------

    def path(self, file: str, row: int, fld: str, value: object) -> str | None:
        checked = safe_rel_path(value, self.limits.max_path_chars)
        if checked is None:
            shown = repr(value)[:80] if isinstance(value, str) else type(value).__name__
            self.report(file, row, fld, f"not a safe relative POSIX path: {shown}", "path")
            return None
        if self.allowed_paths is not None and checked not in self.allowed_paths:
            self.stats[f"{_stem(file)}.paths_not_in_head"] += 1
            return None
        if checked not in self.seen_paths:
            self.seen_paths.add(checked)
            if len(self.seen_paths) > self.limits.max_paths:
                self.report(
                    file, row, fld, f"more than {self.limits.max_paths} distinct paths", "limit", fatal=True
                )
        return checked

    def test_id(self, file: str, row: int, fld: str, value: str, *, known: bool = True) -> bool:
        """Count a test id against the limit; with ``known``, report one tests.jsonl never named."""
        if value == IMPORT_CONTEXT:
            return True
        if value not in self.seen_ids:
            self.seen_ids.add(value)
            if len(self.seen_ids) > self.limits.max_tests:
                self.report(
                    file,
                    row,
                    fld,
                    f"more than {self.limits.max_tests} distinct test ids",
                    "limit",
                    fatal=True,
                )
        if known and value not in self.collected:
            self.report(file, row, fld, f"test {value[:80]!r} is not in tests.jsonl", "unknown_test")
        return True

    def clip(self, file: str, row: int, fld: str, value: str | None, limit: int) -> str | None:
        if value is None or len(value) <= limit:
            return value
        self.report(file, row, fld, f"longer than {limit} characters (clipped)", "clipped")
        return value[: limit - 1] + "…"

    def schema_ok(self, file: str, stem: str, row_no: int, row: dict[str, Any]) -> bool:
        """Whether ``row`` fits its schema; the first real violation drops it and ends the check."""
        try:
            for v in schemas.iter_violations(row, schemas.row_schema(stem, row)):
                if v.keyword == "additionalProperties":
                    self.report(file, row_no, v.path.lstrip("/"), "unknown field (ignored)", "unknown_field")
                    continue
                self.report(file, row_no, v.path.lstrip("/") or None, v.message, "schema")
                return False
        except RecursionError:
            self.report(file, row_no, None, "nested too deep to check", "schema")
            return False
        return True


def _stem(file: str) -> str:
    return file.split(".", 1)[0]


# --- opening ---------------------------------------------------------------------------------------


def _open_source(path: Path, scan: _Scan) -> _Source:
    try:
        st = os.lstat(path)
    except OSError as exc:
        scan.report(str(path.name), None, None, f"cannot read the bundle: {exc.strerror}", "io", fatal=True)
        raise AssertionError from exc  # unreachable: report(fatal=True) raises
    if stat.S_ISDIR(st.st_mode):
        return _dir_source(path, scan)
    if stat.S_ISREG(st.st_mode):
        return _tar_source(path, st.st_size, scan)
    scan.report(
        path.name, None, None, "the bundle is neither a directory nor a regular file", "io", fatal=True
    )
    raise AssertionError  # unreachable


def _dir_source(root: Path, scan: _Scan) -> _Source:
    members: dict[str, int] = {}
    for name in _known_names():
        try:
            st = os.lstat(root / name)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            scan.report(
                name, None, None, "not a regular file (links and devices are refused)", "io", fatal=True
            )
        members[name] = st.st_size
    total = sum(members.values())
    scan.bundle_bytes = total
    if total > scan.limits.max_bundle_bytes:
        scan.report(
            "bundle", None, None, f"{total} bytes (limit {scan.limits.max_bundle_bytes})", "limit", fatal=True
        )
    return _Source(members, None, root)


def _tar_source(path: Path, size: int, scan: _Scan) -> _Source:
    limits = scan.limits
    scan.bundle_bytes = size
    if size > limits.max_bundle_bytes:
        scan.report(
            path.name, None, None, f"{size} bytes (limit {limits.max_bundle_bytes})", "limit", fatal=True
        )
    cap = min(limits.gzip_cap(size), limits.max_bundle_bytes)
    known = set(_known_names())
    data: dict[str, bytes] = {}
    declared = 0
    try:
        with (
            _open_regular(path) as raw,
            gzip.GzipFile(fileobj=raw, mode="rb") as gz,
            tarfile.open(fileobj=_CappedReader(gz, cap), mode="r|") as tar,  # type: ignore[arg-type]
        ):
            for count, member in enumerate(tar, start=1):
                if count > limits.max_members:
                    scan.report(
                        path.name, None, None, f"more than {limits.max_members} members", "limit", fatal=True
                    )
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                if member.isdir() and name.rstrip("/") in ("", "."):
                    continue
                sparse = member.issparse() or any(k.startswith("GNU.sparse.") for k in member.pax_headers)
                if not member.isreg() or sparse:
                    scan.report(
                        path.name,
                        None,
                        None,
                        f"member {name[:80]!r} is not a regular file",
                        "archive",
                        fatal=True,
                    )
                if "/" in name or safe_rel_path(name) is None:
                    scan.report(
                        path.name,
                        None,
                        None,
                        f"member {name[:80]!r} is not a flat name",
                        "archive",
                        fatal=True,
                    )
                if name in data:
                    scan.report(
                        path.name, None, None, f"duplicate member {name[:80]!r}", "archive", fatal=True
                    )
                declared += member.size
                if declared > cap:
                    scan.report(
                        path.name, None, None, f"members declare more than {cap} bytes", "limit", fatal=True
                    )
                if name not in known:
                    continue
                source = tar.extractfile(member)
                if source is None:
                    scan.report(
                        path.name, None, None, f"unreadable member {name[:80]!r}", "archive", fatal=True
                    )
                    raise AssertionError  # unreachable
                data[name] = source.read(member.size)
    except _Capped:
        scan.report(path.name, None, None, f"expands beyond {cap} bytes", "limit", fatal=True)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        scan.report(path.name, None, None, f"corrupt archive: {exc}"[:200], "archive", fatal=True)
    return _Source({name: len(blob) for name, blob in data.items()}, data, None)


# --- per-file readers ----------------------------------------------------------------------------


def _read_manifest(source: _Source, scan: _Scan) -> dict[str, Any]:
    if MANIFEST not in source.members:
        scan.report(MANIFEST, None, None, "missing (every bundle has a manifest)", "missing", fatal=True)
    size = source.members[MANIFEST]
    if size > scan.limits.max_manifest_bytes:
        scan.report(
            MANIFEST,
            None,
            None,
            f"{size} bytes (limit {scan.limits.max_manifest_bytes})",
            "limit",
            fatal=True,
        )
    with source.open(MANIFEST) as fh:
        raw = fh.read(scan.limits.max_manifest_bytes + 1)
    scan.digest.update(f"{MANIFEST}\0{len(raw)}\0".encode())
    scan.digest.update(raw)
    scan.files_read["manifest"] = MANIFEST
    value, error = _decode(raw)
    if error is not None or not isinstance(value, dict):
        scan.report(MANIFEST, None, None, error or "not a JSON object", "json", fatal=True)
    manifest: dict[str, Any] = value  # type: ignore[assignment]
    if manifest.get("format") != FORMAT:
        scan.report(MANIFEST, None, "format", f"must be {FORMAT!r}", "format", fatal=True)
    version = manifest.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != VERSION:
        shown = json.dumps(version)[:40]
        scan.report(
            MANIFEST,
            None,
            "version",
            f"unknown version {shown} (this reader knows {VERSION})",
            "version",
            fatal=True,
        )
    for v in schemas.check(manifest, schemas.SCHEMAS["manifest"]):
        fld = v.path.lstrip("/") or None
        if v.keyword == "additionalProperties":
            scan.report(MANIFEST, None, fld, "unknown field (ignored)", "unknown_field")
        else:
            scan.report(MANIFEST, None, fld, v.message, "schema", fatal=True)
    scan.manifest = manifest
    scan.rows["manifest"] = 1
    return manifest


def _decode(raw: bytes) -> tuple[Any, str | None]:
    """JSON from UTF-8 bytes; refuses NaN/Infinity, invalid UTF-8 and lone surrogates."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not UTF-8"
    try:
        value = json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return None, "not valid JSON"
    if "\\u" in text and ("\\ud" in text.lower()):
        try:
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            return None, "contains a lone surrogate"
    return value, None


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def _rows(source: _Source, scan: _Scan, stem: str) -> Iterator[tuple[str, int, dict[str, Any]]]:
    """``(member name, row number, row)`` for every JSON-object line of one ``.jsonl[.gz]`` file."""
    plain, packed = f"{stem}.jsonl", f"{stem}.jsonl.gz"
    if plain in source.members and packed in source.members:
        scan.report(plain, None, None, f"both {plain} and {packed} are present", "ambiguous", fatal=True)
    name = plain if plain in source.members else packed if packed in source.members else None
    if name is None:
        return
    scan.files_read[stem] = name
    limits = scan.limits
    max_rows = limits.rows_cap(stem)
    scan.rows.setdefault(stem, 0)
    scan.check_clock(name)
    row_no = 0
    fh = source.open(name)
    try:
        with fh:
            size = source.size(name, fh)
            scan.digest.update(f"{name}\0{size}\0".encode())
            hashed = _HashingReader(fh, scan.digest, size)
            stream: Any = hashed
            if name.endswith(".gz"):
                stream = _CappedReader(gzip.GzipFile(fileobj=hashed, mode="rb"), limits.gzip_cap(size))
            while True:
                line = stream.readline(limits.max_json_line_bytes + 1)
                if not line:
                    break
                row_no += 1
                if row_no > max_rows:
                    scan.report(name, row_no, None, f"more than {max_rows} lines", "limit", fatal=True)
                if not row_no & 1023:
                    scan.check_clock(name)
                if len(line) > limits.max_json_line_bytes:
                    scan.report(
                        name,
                        row_no,
                        None,
                        f"line longer than {limits.max_json_line_bytes} bytes",
                        "oversized",
                    )
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(limits.max_json_line_bytes + 1)
                        scan.check_clock(name)
                    continue
                if not line.strip():
                    continue
                scan.rows[stem] += 1
                value, error = _decode(line)
                if error is not None or not isinstance(value, dict):
                    scan.report(name, row_no, None, error or "not a JSON object", "json")
                    continue
                yield name, row_no, value
            hashed.drain()
    except _Capped as capped:
        scan.report(name, None, None, f"expands beyond {capped.cap} bytes", "limit", fatal=True)
    except (OSError, EOFError, zlib.error) as exc:
        what = "corrupt gzip stream" if name.endswith(".gz") else "read error"
        scan.report(name, row_no or None, None, f"{what}: {exc}"[:200], "corrupt", fatal=True)
    finally:
        source.release(name)


def _read_files(source: _Source, scan: _Scan) -> None:
    for name, n, row in _rows(source, scan, "files"):
        if not scan.schema_ok(name, "files", n, row):
            continue
        path = scan.path(name, n, "path", row["path"])
        if path is None:
            continue
        if path in scan.static:
            scan.report(name, n, "path", f"{path!r} is listed twice (later row ignored)", "duplicate")
            continue
        statements = sorted(set(row["statements"]))
        excluded = sorted(set(row.get("excluded", ())))
        branches = sorted({(a, b) for a, b in row.get("branches", ())})
        mentioned = [*statements, *excluded, *(abs(x) for arc in branches for x in arc)]
        n_lines = row.get("n_lines", max(mentioned, default=0))
        beyond = [line for line in statements if line > n_lines]
        if beyond:
            scan.report(
                name,
                n,
                "statements",
                f"{len(beyond)} statement lines beyond n_lines={n_lines}",
                "beyond_file",
            )
            statements = [line for line in statements if line <= n_lines]
        scan.static[path] = StaticFile(path, n_lines, tuple(statements), tuple(excluded), tuple(branches))
        scan.statement_sets[path] = frozenset(statements)


def _read_tests(source: _Source, scan: _Scan) -> None:
    limits = scan.limits
    for name, n, row in _rows(source, scan, "tests"):
        if not scan.schema_ok(name, "tests", n, row):
            continue
        test_id = row["id"]
        if test_id == IMPORT_CONTEXT:
            scan.report(name, n, "id", f"{IMPORT_CONTEXT} is reserved for coverage outside tests", "reserved")
            continue
        if test_id in scan.collected:
            scan.report(
                name, n, "id", f"test {test_id[:80]!r} is listed twice (later row ignored)", "duplicate"
            )
            continue
        file = scan.path(name, n, "file", row["file"])
        if file is None:
            continue
        scan.test_id(name, n, "id", test_id, known=False)
        scan.collected[test_id] = CollectedItem(
            nodeid=test_id,
            file=file,
            line=row.get("line"),
            markers=tuple(row.get("markers", ())),
            invariant_ids=tuple(row.get("invariants", ())),
            hypothesis=row.get("property", False),
        )
        outcome = row["outcome"]
        if outcome is None:
            continue
        duration = row.get("duration_s")
        phase = row.get("failed_phase")
        scan.tests[test_id] = TestRow(
            nodeid=test_id,
            file=file,
            outcome=outcome,
            duration_s=float(duration) if duration is not None else 0.0,
            failed_phase=phase if phase in _PHASES else None,
            fail_sig=scan.clip(name, n, "fail_sig", row.get("fail_sig"), 64),
            longrepr=scan.clip(name, n, "message", row.get("message"), limits.max_longrepr_chars),
            start=_float(row.get("start")),
            stop=_float(row.get("stop")),
        )
        if duration is not None:
            scan.durations[test_id] = float(duration)


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _read_links(source: _Source, scan: _Scan) -> None:
    for name, n, row in _rows(source, scan, "links"):
        if not scan.schema_ok(name, "links", n, row):
            continue
        if row["test"] not in scan.collected:
            scan.report(name, n, "test", f"test {row['test'][:80]!r} is not in tests.jsonl", "unknown_test")
            continue
        scan.links.setdefault(row["test"], set()).add(row["invariant"])


def _read_coverage(source: _Source, scan: _Scan) -> None:
    limits = scan.limits
    for name, n, row in _rows(source, scan, "coverage"):
        if not scan.schema_ok(name, "coverage", n, row):
            continue
        test_id = row["test"]
        path = scan.path(name, n, "file", row["file"])
        if path is None:
            continue
        scan.test_id(name, n, "test", test_id)
        lines = set(row["lines"])  # one row's lines only; what is kept is a sorted tuple
        statements = scan.statement_sets.get(path)
        if statements is None:
            scan.report(name, n, "file", f"{path!r} is not in files.jsonl", "unknown_file")
        else:
            outside = lines - statements
            if outside:
                shown = ", ".join(map(str, sorted(outside)[:5]))
                scan.report(
                    name,
                    n,
                    "lines",
                    f"{len(outside)} lines are not statements of {path} ({shown})",
                    "not_statement",
                )
                lines &= statements
        by_path = scan.coverage.setdefault(test_id, {})
        before = by_path.get(path)
        if before is not None:
            scan.report(name, n, None, f"({test_id[:60]!r}, {path!r}) is listed twice (merged)", "duplicate")
            lines -= set(before)
            if not lines:
                continue
            lines |= set(before)
        added = len(lines) - (len(before) if before is not None else 0)
        ints = scan.ints
        by_path[path] = tuple([ints.setdefault(line, line) for line in sorted(lines)])
        scan.cells += added
        if scan.cells > limits.max_coverage_cells:
            scan.report(
                name, n, None, f"more than {limits.max_coverage_cells} coverage cells", "limit", fatal=True
            )


def _read_arcs(source: _Source, scan: _Scan) -> None:
    for name, n, row in _rows(source, scan, "arcs"):
        if not scan.schema_ok(name, "arcs", n, row):
            continue
        path = scan.path(name, n, "file", row["file"])
        if path is None:
            continue
        if path not in scan.static:
            scan.report(name, n, "file", f"{path!r} is not in files.jsonl", "unknown_file")
        if path in scan.run_arcs:
            scan.report(name, n, "file", f"{path!r} is listed twice (merged)", "duplicate")
        scan.run_arcs.setdefault(path, set()).update((a, b) for a, b in row["arcs"])


def _read_functions(source: _Source, scan: _Scan) -> None:
    for name, n, row in _rows(source, scan, "functions"):
        if not scan.schema_ok(name, "functions", n, row):
            continue
        path = scan.path(name, n, "path", row["path"])
        if path is None:
            continue
        qualname, ordinal = row["qualname"], row["ordinal"]
        key = func_key(path, qualname, ordinal)
        if "key" in row and row["key"] != key:
            scan.report(
                name, n, "key", f"is not blake2b(path, qualname, ordinal) = {key} (recomputed)", "key"
            )
        if not row["def_line"] <= row["body_start"] <= row["end_line"]:
            scan.report(name, n, None, "needs def_line <= body_start <= end_line", "lines")
            continue
        identity = (path, qualname, ordinal)
        if identity in scan.functions:
            scan.report(
                name,
                n,
                None,
                f"{qualname!r} ordinal {ordinal} is listed twice (later row ignored)",
                "duplicate",
            )
            continue
        if path not in scan.static:
            scan.report(name, n, "path", f"{path!r} is not in files.jsonl", "unknown_file")
        if len(scan.functions) >= scan.limits.max_functions:
            scan.report(
                name, n, None, f"more than {scan.limits.max_functions} functions", "limit", fatal=True
            )
        params = tuple(row["params"])
        region = FunctionRegion(
            func_key=key,
            path=path,
            qualname=qualname,
            ordinal=ordinal,
            def_line=row["def_line"],
            body_start=row["body_start"],
            end_line=row["end_line"],
            body_hash=row["body_hash"],
            sig_hash=row["sig_hash"],
            params=params,
            kind=row["kind"],
        )
        scan.functions[identity] = region
        if "types" in row:
            scan.types[region.triple] = _types(scan, name, n, row["types"], params)


def _types(
    scan: _Scan, name: str, n: int, raw: Mapping[str, Any], params: tuple[str, ...]
) -> FrozenMap[str, TypeDescriptor | None]:
    known = {p.lstrip("*") for p in params} | {"return"}
    found: dict[str, TypeDescriptor | None] = {}
    for param, value in raw.items():
        if param.lstrip("*") not in known:
            scan.report(
                name, n, f"types/{param[:40]}", "not a parameter of the function (ignored)", "unknown_param"
            )
            continue
        if value is None:
            found[param] = None
            continue
        try:
            found[param] = from_json(value)
        except DescriptorError as exc:
            scan.report(name, n, f"types/{param[:40]}", str(exc), "descriptor")
    return FrozenMap(found)


def _triple(scan: _Scan, name: str, n: int, fld: str, value: list[Any]) -> FuncTriple | None:
    path = scan.path(name, n, fld, value[0])
    return (path, value[1], value[2]) if path is not None else None


def _read_shapes(source: _Source, scan: _Scan) -> None:
    limits = scan.limits
    for name, n, row in _rows(source, scan, "shapes"):
        kind = row.get("kind")
        if kind not in ("obs", "sampling"):
            scan.report(name, n, "kind", "must be 'obs' or 'sampling'", "schema")
            continue
        if not scan.schema_ok(name, "shapes", n, row):
            continue
        func = _triple(scan, name, n, "func", row["func"])
        if func is None:
            continue
        for test in row["tests"]:
            scan.test_id(name, n, "tests", test)
        if kind == "sampling":
            cap, truncated = row.get("cap"), row.get("truncated", False)
            if cap is not None and not truncated:
                scan.report(name, n, "cap", "a cap stopped sampling, so truncated must be true", "cap")
            scan.sampling.append(FuncSampling(func, row["calls"], tuple(row["tests"]), truncated, cap))
            continue
        problems = shape_keys.validate(row["shape"])
        if problems:
            scan.report(name, n, "shape", "; ".join(problems)[:300], "shape_key")
            continue
        if row["shape"] not in scan.seen_keys:
            scan.seen_keys.add(row["shape"])
            if len(scan.seen_keys) > limits.max_shape_keys:
                scan.report(
                    name,
                    n,
                    "shape",
                    f"more than {limits.max_shape_keys} distinct shape keys",
                    "limit",
                    fatal=True,
                )
        raw_caller = row["caller"]
        caller: FuncTriple | str | None
        if isinstance(raw_caller, dict):
            caller = raw_caller["test"]
            scan.test_id(name, n, "caller", raw_caller["test"])
        else:
            caller = _triple(scan, name, n, "caller", raw_caller)
            if caller is None:
                continue
        example_test = row.get("example_test")
        if example_test is not None:
            scan.test_id(name, n, "example_test", example_test)
        scan.shapes.append(
            ShapeObs(
                func=func,
                param=row["param"],
                shape_key=row["shape"],
                caller=caller,
                samples=row["samples"],
                tests=tuple(row["tests"]),
                truncated=row.get("truncated", False),
                example=scan.clip(name, n, "example", row.get("example"), limits.max_repr_chars),
                example_test=example_test,
            )
        )


def _scan(path: Path, scan: _Scan) -> None:
    source = _open_source(Path(path), scan)
    manifest = _read_manifest(source, scan)
    for stem in REQUIRED_FILES:
        if f"{stem}.jsonl" not in source.members and f"{stem}.jsonl.gz" not in source.members:
            scan.report(f"{stem}.jsonl", None, None, "missing (a required file)", "missing", fatal=True)
    language = manifest.get("language")
    has_functions = "functions.jsonl" in source.members or "functions.jsonl.gz" in source.members
    if not has_functions and language not in LANGUAGES_WITH_SOURCE_INDEX:
        scan.report(
            "functions.jsonl",
            None,
            None,
            f"missing: required for {language!r}, whose adapter cannot index functions from the sources",
            "missing",
        )
    _read_files(source, scan)
    _read_tests(source, scan)
    _read_links(source, scan)
    _read_coverage(source, scan)
    _read_arcs(source, scan)
    _read_functions(source, scan)
    _read_shapes(source, scan)


# --- public API ------------------------------------------------------------------------------------


def inspect(path: Path | str, *, limits: Limits | None = None) -> BundleInfo:
    """Scan a bundle and report everything found: manifest, rows per file, problems, drops.

    Never raises on bad input: a fatal problem ends the scan and is the last problem listed.
    """
    scan = _Scan(limits or Limits(), collect=True)
    try:
        _scan(Path(path), scan)
    except BundleRejected:
        pass
    except (OSError, RecursionError, MemoryError) as exc:
        scan.problems.append(Problem("bundle", None, None, f"unreadable: {exc}"[:200], "io", fatal=True))
    if scan.overflow:
        scan.problems.append(
            Problem("bundle", None, None, f"... and {scan.overflow} more problems", "overflow")
        )
    return BundleInfo(
        manifest=scan.manifest,
        files=dict(scan.files_read),
        rows=dict(scan.rows),
        problems=scan.problems,
        dropped={k: v for k, v in sorted(scan.stats.items())},
        bundle_bytes=scan.bundle_bytes,
    )


def validate(path: Path | str, *, limits: Limits | None = None) -> list[Problem]:
    """Every problem with the bundle at ``path`` (a directory or ``tar.gz``); empty when valid."""
    return inspect(path, limits=limits).problems


def read_manifest(path: Path | str) -> dict[str, Any] | None:
    """The manifest of a neutral bundle directory, or None when ``path`` holds no neutral manifest.

    Reads at most 1 MiB of ``manifest.json`` (never through a symlink) and only checks ``format``.
    """
    try:
        with _open_regular(Path(path) / MANIFEST) as fh:
            raw = fh.read(Limits().max_manifest_bytes + 1)
    except OSError:
        return None
    value, error = _decode(raw)
    if error is not None or not isinstance(value, dict) or value.get("format") != FORMAT:
        return None
    return value


def is_neutral(path: Path | str) -> bool:
    """Whether the directory ``path`` holds a neutral bundle (its ``manifest.json`` says so)."""
    return read_manifest(path) is not None


def read_bundle(
    path: Path | str, *, limits: Limits | None = None, allowed_paths: Collection[str] | None = None
) -> NeutralBundle:
    """Read the bundle at ``path`` (a directory or a ``tar.gz``) into a :class:`NeutralBundle`.

    ``allowed_paths`` (optional) is the set of files in the head tree: rows naming any other path
    are dropped and counted (``<file>.paths_not_in_head``). Raises :class:`BundleRejected` when a
    hard limit is broken or the bundle cannot be read; softer problems drop the offending row and
    are counted in ``meta["convert"]["dropped"]``.
    """
    scan = _Scan(
        limits or Limits(),
        collect=False,
        allowed_paths=frozenset(allowed_paths) if allowed_paths is not None else None,
    )
    try:
        _scan(Path(path), scan)
    except BundleRejected:
        raise
    except (OSError, RecursionError, MemoryError) as exc:
        raise BundleRejected(Problem("bundle", None, None, f"unreadable: {exc}"[:200], "io", True)) from exc
    return _build(scan)


def _build(scan: _Scan) -> NeutralBundle:
    manifest = scan.manifest or {}
    collected = tuple(
        replace(
            item, invariant_ids=tuple(sorted(set(item.invariant_ids) | scan.links.get(item.nodeid, set())))
        )
        if item.nodeid in scan.links
        else item
        for item in scan.collected.values()
    )
    meta = dict(manifest.get("meta") or {})
    meta["bundle"] = {
        key: manifest.get(key) for key in ("format", "version", "language", "producer", "created_at", "shard")
    }
    meta["convert"] = {
        "bundle_sha256": scan.digest.hexdigest(),
        "files": sorted(scan.files_read.values()),
        "rows": dict(scan.rows),
        "dropped": {k: v for k, v in sorted(scan.stats.items())},
        "bundle_bytes": scan.bundle_bytes,
        "contexts": len(scan.coverage),
        "head_checked": scan.allowed_paths is not None,
    }
    coverage = {
        test: {path: lines for path, lines in by_path.items() if lines}
        for test, by_path in scan.coverage.items()
    }
    return NeutralBundle(
        meta=meta,
        collected=collected,
        tests=tuple(scan.tests.values()),
        coverage={test: by_path for test, by_path in coverage.items() if by_path},
        static=dict(scan.static),
        run_arcs={path: tuple(sorted(arcs)) for path, arcs in scan.run_arcs.items()},
        shapes=tuple(scan.shapes),
        sampling=tuple(scan.sampling),
        durations=dict(scan.durations),
        functions=tuple(scan.functions.values()),
        types=FrozenMap(scan.types),
    )


# --- writing ---------------------------------------------------------------------------------------


def _clip(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _caller_order(caller: FuncTriple | str) -> tuple[int, str, str, int]:
    return (0, caller, "", 0) if isinstance(caller, str) else (1, *caller)


def conform(bundle: NeutralBundle, *, limits: Limits | None = None) -> NeutralBundle:
    """``bundle`` as the on-disk format keeps it, so ``read_bundle(write_bundle(b)) == conform(b)``.

    Tests and collected items are sorted by id and every test with a result is also a collected
    item; a test's duration is its ``durations`` entry; executed lines that are not statements of
    a file in ``static`` are dropped; failure messages and examples are clipped; observations with
    a shape key outside the grammar are dropped; shapes, sampling rows and functions are sorted;
    ``types`` keeps the entries of shipped functions. ``meta`` is unchanged (the reader replaces
    the keys in :data:`READER_META_KEYS`).
    """
    limits = limits or Limits()
    items = {item.nodeid: item for item in bundle.collected}
    for row in bundle.tests:
        items.setdefault(row.nodeid, CollectedItem(row.nodeid, row.file, None, (), (), False))
    collected = tuple(
        replace(item, invariant_ids=tuple(sorted(set(item.invariant_ids))))
        for _, item in sorted(items.items())
    )
    durations = {row.nodeid: bundle.durations.get(row.nodeid, row.duration_s) for row in bundle.tests}
    tests = tuple(
        replace(
            row,
            file=items[row.nodeid].file,
            duration_s=durations[row.nodeid],
            fail_sig=_clip(row.fail_sig, 64),
            longrepr=_clip(row.longrepr, limits.max_longrepr_chars),
        )
        for row in sorted(bundle.tests, key=lambda r: r.nodeid)
    )
    static = {
        path: StaticFile(
            facts.path,
            facts.n_lines,
            tuple(sorted(set(facts.statements))),
            tuple(sorted(set(facts.excluded))),
            tuple(sorted(set(facts.arcs_possible))),
        )
        for path, facts in sorted(bundle.static.items())
    }
    coverage: dict[str, dict[str, tuple[int, ...]]] = {}
    for test, by_path in bundle.coverage.items():
        kept: dict[str, tuple[int, ...]] = {}
        for path, lines in by_path.items():
            allowed = static[path].statements if path in static else None
            chosen = sorted(set(lines) & set(allowed)) if allowed is not None else sorted(set(lines))
            if chosen:
                kept[path] = tuple(chosen)
        if kept:
            coverage[test] = kept
    shapes = tuple(
        sorted(
            (
                replace(obs, example=_clip(obs.example, limits.max_repr_chars))
                for obs in bundle.shapes
                if shape_keys.is_valid(obs.shape_key)
            ),
            key=lambda o: (o.func, o.param, o.shape_key, _caller_order(o.caller), o.tests, o.example or ""),
        )
    )
    functions = tuple(sorted(bundle.functions, key=lambda r: (r.path, r.def_line, r.qualname, r.ordinal)))
    triples = {r.triple for r in functions}
    return NeutralBundle(
        meta=bundle.meta,
        collected=collected,
        tests=tests,
        coverage=coverage,
        static=static,
        run_arcs={path: tuple(sorted(set(arcs))) for path, arcs in sorted(bundle.run_arcs.items())},
        shapes=shapes,
        sampling=tuple(sorted(bundle.sampling, key=lambda s: (s.func, s.calls))),
        durations=durations,
        functions=functions,
        types=FrozenMap(
            {t: FrozenMap(dict(by_param)) for t, by_param in bundle.types.items() if t in triples}
        ),
    )


def _manifest_for(bundle: NeutralBundle, manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    from coverset_model import __version__

    recorded = bundle.meta.get("bundle") if isinstance(bundle.meta, dict) else None
    base = dict(recorded) if isinstance(recorded, dict) else {}
    given = dict(manifest or {})
    meta = given.pop("meta", None)
    if meta is None:
        meta = {k: v for k, v in bundle.meta.items() if k not in READER_META_KEYS}
    merged = {**base, **given}
    producer = merged.get("producer") or {"name": "coverset-model", "version": __version__}
    return {
        "format": FORMAT,
        "version": VERSION,
        "language": merged.get("language") or "python",
        "producer": {"name": str(producer.get("name")), "version": str(producer.get("version"))},
        "created_at": merged.get("created_at"),
        "shard": merged.get("shard"),
        "meta": meta,
    }


def _json_line(row: Mapping[str, Any]) -> bytes:
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _test_rows(bundle: NeutralBundle) -> Iterator[dict[str, Any]]:
    results = {row.nodeid: row for row in bundle.tests}
    for item in bundle.collected:
        result = results.get(item.nodeid)
        yield {
            "id": item.nodeid,
            "file": item.file,
            "line": item.line,
            "outcome": result.outcome if result else None,
            "duration_s": bundle.durations.get(item.nodeid) if result else None,
            "failed_phase": result.failed_phase if result else None,
            "fail_sig": result.fail_sig if result else None,
            "message": result.longrepr if result else None,
            "markers": list(item.markers),
            "invariants": list(item.invariant_ids),
            "property": item.hypothesis,
            "start": result.start if result else None,
            "stop": result.stop if result else None,
        }


def _function_rows(bundle: NeutralBundle) -> Iterator[dict[str, Any]]:
    for region in bundle.functions:
        row: dict[str, Any] = {
            "key": region.func_key,
            "path": region.path,
            "qualname": region.qualname,
            "ordinal": region.ordinal,
            "def_line": region.def_line,
            "body_start": region.body_start,
            "end_line": region.end_line,
            "body_hash": region.body_hash,
            "sig_hash": region.sig_hash,
            "params": list(region.params),
            "kind": region.kind,
        }
        declared = bundle.types.get(region.triple)
        if declared is not None:
            row["types"] = {p: (to_json(d) if d is not None else None) for p, d in declared.items()}
        yield row


def _shape_rows(bundle: NeutralBundle) -> Iterator[dict[str, Any]]:
    for obs in bundle.shapes:
        yield {
            "kind": "obs",
            "func": list(obs.func),
            "param": obs.param,
            "shape": obs.shape_key,
            "caller": {"test": obs.caller} if isinstance(obs.caller, str) else list(obs.caller),
            "samples": obs.samples,
            "tests": list(obs.tests),
            "truncated": obs.truncated,
            "example": obs.example,
            "example_test": obs.example_test,
        }
    for row in bundle.sampling:
        yield {
            "kind": "sampling",
            "func": list(row.func),
            "calls": row.calls,
            "tests": list(row.tests),
            "truncated": row.truncated,
            "cap": row.cap,
        }


def write_bundle(
    bundle: NeutralBundle,
    directory: Path | str,
    *,
    manifest: Mapping[str, Any] | None = None,
    compress: bool = False,
    limits: Limits | None = None,
) -> Path:
    """Write ``conform(bundle)`` as a neutral bundle directory; returns the directory.

    ``manifest`` supplies ``language``, ``producer``, ``created_at``, ``shard`` and optionally
    ``meta``; anything not given comes from ``bundle.meta["bundle"]`` (a bundle that was read from
    disk), else ``language="python"``, this package as producer, and ``created_at``/``shard`` null.
    ``meta`` defaults to ``bundle.meta`` without the reader's own keys. ``compress`` writes
    ``.jsonl.gz`` files (gzip with no timestamp, so the bytes are reproducible). Every bundle file
    left in ``directory`` from an earlier write (the other variant, an optional file with no rows
    now, ``links.jsonl``, which this writer never produces) is removed. Each file is created
    afresh after its old name is unlinked, never through a symlink (``O_EXCL | O_NOFOLLOW``).
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = conform(bundle, limits=limits)
    head = _manifest_for(bundle, manifest)
    with _create(directory / MANIFEST) as fh:
        fh.write((json.dumps(head, ensure_ascii=False, indent=1, allow_nan=False) + "\n").encode("utf-8"))
    files: dict[str, Iterator[dict[str, Any]] | list[dict[str, Any]]] = {
        "tests": _test_rows(data),
        "files": [
            {
                "path": s.path,
                "n_lines": s.n_lines,
                "statements": list(s.statements),
                "excluded": list(s.excluded),
                "branches": [list(a) for a in s.arcs_possible],
            }
            for s in data.static.values()
        ],
        "coverage": [
            {"test": test, "file": path, "lines": list(lines)}
            for test in sorted(data.coverage, key=lambda t: (t != IMPORT_CONTEXT, t))
            for path, lines in sorted(data.coverage[test].items())
        ],
        "arcs": [{"file": path, "arcs": [list(a) for a in arcs]} for path, arcs in data.run_arcs.items()],
        "functions": _function_rows(data),
        "shapes": _shape_rows(data),
    }
    for stem in STEMS:
        for stale in (directory / f"{stem}.jsonl", directory / f"{stem}.jsonl.gz"):
            stale.unlink(missing_ok=True)
    for stem, rows in files.items():
        blob = b"".join(_json_line(row) for row in rows)
        if not blob and stem in OPTIONAL_FILES:
            continue
        if compress:
            with (
                _create(directory / f"{stem}.jsonl.gz") as raw,
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
            ):
                gz.write(blob)
        else:
            with _create(directory / f"{stem}.jsonl") as raw:
                raw.write(blob)
    return directory


def _create(path: Path) -> IO[bytes]:
    """A new regular file at ``path`` (an old file or link there is unlinked first, never followed)."""
    path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o644), "wb")


def pack(directory: Path | str, archive: Path | str) -> Path:
    """Pack a bundle directory's known files into a flat, reproducible ``tar.gz`` at ``archive``."""
    directory, archive = Path(directory), Path(archive)
    with (
        _create(archive) as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for name in _known_names():
            path = directory / name
            try:
                with _open_regular(path) as fh:
                    data = fh.read()
            except OSError:  # absent, a link or not a regular file: not packed
                continue
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    return archive
