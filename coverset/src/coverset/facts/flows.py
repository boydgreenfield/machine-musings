"""Production flows in the facts store: validate ``prod-flows`` v1 payloads, store them, read them back.

A payload is one export window of ``coverset-probe`` (``packages/coverset-probe``): per
``(function, parameter, shape key)`` the estimated calls and distinct traces, per call vector the
same, per request flow the set of functions it called, and the exact call count of every observed
function (``docs/production-flows.md``; JSON Schema: :data:`PROD_FLOWS_SCHEMA`, mirrored in
``docs/schema/prod-flows.v1.json``).

**Layout.** ``coverset flows import`` writes one *batch* directory per imported file and service::

    <facts_dir>/repo=<repo_id>/prod/service=<service>/batch=<utc stamp>-<sha12>/
        prod_windows.parquet  prod_funcs.parquet  prod_shapes.parquet
        prod_calls.parquet    prod_flows.parquet

The tables (``coverset.facts.schema.PROD_SCHEMAS``) carry ``window_start``/``window_end`` on every
row, so a batch may hold many windows. The directory is assembled under a hidden name and renamed
into place, so a reader sees a whole batch or nothing; imports of one repo serialise on
``prod/.lock``. A window whose payload (sha256 of its canonical JSON) was imported before is skipped,
so importing the same file twice is a no-op. Nothing here lives in a ``run=`` directory: run views,
``coverset gc`` and the run inventories never see production data, and ``coverset flows prune``
applies its own retention.

**Trust.** A payload comes from a production service, through whatever transport the operator
chose, so it is validated like a shard bundle: a size cap per file and per line, the JSON Schema,
function indices in range, every shape key checked against the version-1 grammar
(:func:`coverset_model.shape_keys.validate`), no more than :data:`MAX_ROWS` rows. A window that
fails is rejected whole and reported; the others in the file are still imported. Each row is stored
with ``k_ok``: a producer that gates (its ``probe.k`` is set, as the probe's is) exported only rows
that passed its lifetime k-anonymity gate, so they are ``k_ok``; rows of any other producer are
``k_ok`` only with at least :data:`DEFAULT_K` distinct traces in their own window, and the readers
(``gaps``, ``archetypes``) skip the rest.

**Function identity.** The probe names functions by module and qualname (plus a path relative to the
service's source root when it knew one). :func:`resolve_functions` maps them onto a run's function
regions: by ``(path, qualname)`` when a path is given, else by matching the module against the
dotted names a region's path can import as; several same-qualname regions in one file are told apart
by the closest first line. Unresolved functions keep a null ``func_key`` and are resolved again when
the data is read against another run. :func:`caller_index` inverts the probe's caller hash
(blake2b-8 of ``module\\0qualname``) over the same regions.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import re
import shutil
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import jsonschema
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from coverset.facts.schema import PROD_SCHEMAS, PROD_TABLES
from coverset.facts.store import FactsStore
from coverset.facts.untrusted import BundleError, CappedReader, open_regular
from coverset.lang.base import FunctionRegion
from coverset_model.shape_keys import validate as validate_shape_key

__all__ = [
    "MAX_FILE_BYTES",
    "MAX_LINE_BYTES",
    "MAX_ROWS",
    "PROD_FLOWS_SCHEMA",
    "FlowsError",
    "FunctionResolver",
    "ImportResult",
    "ProdData",
    "WindowInfo",
    "caller_hash",
    "caller_index",
    "import_payloads",
    "list_windows",
    "prune_windows",
    "read_payload_file",
    "payload_sha256",
    "prod_dir",
    "problems_summary",
    "read_prod",
    "resolve_functions",
    "run_functions_reached",
    "run_has_shapes",
    "run_shapes_by_function",
    "select_windows",
    "validate_payload",
]

MAX_FILE_BYTES: Final = 512 * 1024 * 1024
"""A flows file (decompressed) is refused past this."""
MAX_LINE_BYTES: Final = 64 * 1024 * 1024
"""One window (one JSON line) is refused past this."""
MAX_ROWS: Final = 1_000_000
"""Rows of one kind (``rows``, ``calls``, ``flows``) per window."""
PROD_DIR: Final = "prod"
DEFAULT_K: Final = 5
"""The k a row of a non-gating producer (no ``probe.k``) must reach within its own window."""
_SERVICE: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SERVICE_RE: Final = re.compile(_SERVICE)
_ENV: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_TIME: Final = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
_CALLER: Final = r"^([0-9a-f]{16}|<external>|<other>)$"
_BATCH_RE: Final = re.compile(r"^batch=[0-9TZ]+-[0-9a-f]{12}$")
_COUNT: Final = {"type": "number", "minimum": 0, "maximum": 1e15}
_INT_COUNT: Final = {"type": "integer", "minimum": 0, "maximum": 10**15}
_INDEX: Final = {"type": "integer", "minimum": 0, "maximum": 10_000_000}
_PARAM: Final = {"type": "string", "minLength": 1, "maxLength": 256}
_KEY: Final = {"type": "string", "minLength": 1, "maxLength": 512}
_NAME: Final = {"type": "string", "minLength": 1, "maxLength": 512}

PROD_FLOWS_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://coverset.dev/schema/prod-flows/v1.json",
    "title": "prod-flows v1: one export window of coverset-probe (shape keys and counts, never values)",
    "type": "object",
    "required": ["version", "kind", "service", "env", "window", "funcs", "rows"],
    "properties": {
        "version": {"const": 1},
        "kind": {"const": "coverset.prod-flows"},
        "service": {"type": "string", "pattern": _SERVICE},
        "env": {"type": "string", "pattern": _ENV},
        "release": {"type": ["string", "null"], "maxLength": 128},
        "window": {
            "type": "object",
            "required": ["start", "end"],
            "properties": {
                "start": {"type": "string", "pattern": _TIME},
                "end": {"type": "string", "pattern": _TIME},
            },
            "additionalProperties": False,
        },
        "probe": {"type": "object", "maxProperties": 64},
        "funcs": {
            "type": "array",
            "maxItems": 100_000,
            "items": {
                "type": "object",
                "required": ["module", "qualname"],
                "properties": {
                    "module": _NAME,
                    "qualname": _NAME,
                    "line": {"type": "integer", "minimum": 0, "maximum": 10_000_000},
                    "path": {"type": ["string", "null"], "maxLength": 512},
                    "calls": _INT_COUNT,
                    "sampled": _INT_COUNT,
                    "capped": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "rows": {
            "type": "array",
            "maxItems": MAX_ROWS,
            "items": {
                "type": "object",
                "required": ["f", "p", "s", "calls"],
                "properties": {
                    "f": _INDEX,
                    "p": _PARAM,
                    "s": _KEY,
                    "calls": _COUNT,
                    "traces": _INT_COUNT,
                    "k_ok": {"type": "boolean"},
                    "callers": {
                        "type": "object",
                        "maxProperties": 16,
                        "propertyNames": {"pattern": _CALLER},
                        "additionalProperties": _COUNT,
                    },
                },
                "additionalProperties": False,
            },
        },
        "calls": {
            "type": "array",
            "maxItems": MAX_ROWS,
            "items": {
                "type": "object",
                "required": ["f", "v", "calls"],
                "properties": {
                    "f": _INDEX,
                    "v": {
                        "type": "array",
                        "maxItems": 256,
                        "items": {
                            "type": "array",
                            "prefixItems": [_PARAM, _KEY],
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    },
                    "calls": _COUNT,
                    "traces": _INT_COUNT,
                    "k_ok": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "flows": {
            "type": "array",
            "maxItems": MAX_ROWS,
            "items": {
                "type": "object",
                "required": ["fs"],
                "properties": {
                    "fs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1024,
                        "uniqueItems": True,
                        "items": _INDEX,
                    },
                    "traces": _INT_COUNT,
                    "k_ok": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}
"""The ``prod-flows`` v1 payload. ``docs/schema/prod-flows.v1.json`` is this dict as JSON (a test
keeps them equal; regenerate with ``uv run python -c "import json; from coverset.facts.flows import
PROD_FLOWS_SCHEMA as S; print(json.dumps(S, indent=2))" > docs/schema/prod-flows.v1.json``)."""

_VALIDATOR: Final = jsonschema.Draft202012Validator(PROD_FLOWS_SCHEMA)


class FlowsError(ValueError):
    """A flows file cannot be read at all (missing, not a regular file, over the size cap)."""


def caller_hash(module: str, qualname: str) -> str:
    """The probe's caller id: blake2b-8 of ``module\\0qualname`` as 16 hex digits (``coverset_probe.tracer``)."""
    return hashlib.blake2b(f"{module}\0{qualname}".encode(), digest_size=8).hexdigest()


# -- reading and validating ------------------------------------------------------------------------


def read_payload_file(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """``(line number, payload or None, problem or None)`` for each non-blank line of a flows file.

    ``.gz`` files are decompressed (capped at :data:`MAX_FILE_BYTES`). Raises :class:`FlowsError`
    when the file cannot be opened or is over the cap.
    """
    try:
        raw = open_regular(path)
    except BundleError as exc:
        raise FlowsError(str(exc)) from None
    with raw:
        stream: Any = gzip.GzipFile(fileobj=raw) if path.suffix == ".gz" else raw
        reader = CappedReader(stream, MAX_FILE_BYTES, path.name)
        number = 0
        try:
            while True:
                line = reader.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                number += 1
                if len(line) > MAX_LINE_BYTES:
                    yield number, None, f"line is longer than {MAX_LINE_BYTES} bytes"
                    while line and not line.endswith(b"\n"):
                        line = reader.readline(MAX_LINE_BYTES + 1)
                    continue
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except (ValueError, RecursionError) as exc:
                    yield number, None, f"not JSON: {str(exc)[:200]}"
                    continue
                if not isinstance(payload, dict):
                    yield number, None, "not a JSON object"
                    continue
                yield number, payload, None
        except (OSError, EOFError, BundleError) as exc:
            raise FlowsError(f"{path.name}: {exc}") from None


_ROW_KINDS: Final = ("rows", "calls", "flows")
_CALLER_RE: Final = re.compile(_CALLER)


def validate_payload(payload: Mapping[str, Any], *, full_schema: bool = False) -> list[str]:
    """Problems with one payload (empty when it can be imported): schema, then the rules it cannot say.

    The envelope (everything but ``rows``, ``calls`` and ``flows``) goes through the JSON Schema;
    the row arrays, which can hold a million entries, through :func:`_row_problems`, a hand-written
    check of exactly the schema's row rules that runs about 20 times faster than ``jsonschema``.
    ``full_schema`` runs the JSON Schema over everything instead (the tests hold both to the same
    verdicts).
    """
    if full_schema or not isinstance(payload, Mapping):
        subject: Any = payload
    else:
        subject = {k: ([] if k in _ROW_KINDS and isinstance(v, list) else v) for k, v in payload.items()}
    problems = [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message[:300]}"
        for error in sorted(_VALIDATOR.iter_errors(subject), key=lambda e: list(map(str, e.absolute_path)))[
            :20
        ]
    ]
    if not problems and not full_schema:
        problems = _row_problems(payload)
    if problems:
        return problems
    n_funcs = len(payload["funcs"])
    start, end = payload["window"]["start"], payload["window"]["end"]
    try:  # the schema's pattern accepts "2026-13-45T..."; the calendar does not
        start_time, end_time = _parse_time(start), _parse_time(end)
    except ValueError:
        return ["window: start/end is not a valid UTC time"]
    if end_time < start_time:
        problems.append("window: end is before start")
    for kind in ("rows", "calls"):
        for i, row in enumerate(payload.get(kind) or ()):
            if row["f"] >= n_funcs:
                problems.append(f"{kind}/{i}/f: no function {row['f']} (funcs has {n_funcs})")
    for i, flow in enumerate(payload.get("flows") or ()):
        if any(f >= n_funcs for f in flow["fs"]):
            problems.append(f"flows/{i}/fs: a function index is out of range")
    keys = {row["s"] for row in payload["rows"]}
    keys |= {pair[1] for row in payload.get("calls") or () for pair in row["v"]}
    for key in sorted(keys):
        for problem in validate_shape_key(key):
            problems.append(f"shape key {key[:60]!r}: {problem}")
            break
    return problems[:50]


def _is_int(value: object, high: int) -> bool:
    return type(value) is int and 0 <= value <= high


def _is_count(value: object) -> bool:
    return (type(value) is int or type(value) is float) and 0 <= value <= 1e15  # NaN fails both bounds


def _is_text(value: object, high: int) -> bool:
    return type(value) is str and 0 < len(value) <= high


_ROW_FIELDS: Final = {
    "rows": (
        frozenset({"f", "p", "s", "calls"}),
        frozenset({"f", "p", "s", "calls", "traces", "k_ok", "callers"}),
    ),
    "calls": (frozenset({"f", "v", "calls"}), frozenset({"f", "v", "calls", "traces", "k_ok"})),
    "flows": (frozenset({"fs"}), frozenset({"fs", "traces", "k_ok"})),
}


def _row_problem(kind: str, row: object) -> str | None:
    """What is wrong with one row of ``kind`` under :data:`PROD_FLOWS_SCHEMA`, or None."""
    if type(row) is not dict:
        return "is not an object"
    required, allowed = _ROW_FIELDS[kind]
    if not required <= row.keys():
        return f"misses {sorted(required - row.keys())}"
    if not row.keys() <= allowed:
        return f"Additional properties are not allowed ({sorted(row.keys() - allowed)})"
    if "traces" in row and not _is_int(row["traces"], 10**15):
        return "traces: not a count"
    if "k_ok" in row and type(row["k_ok"]) is not bool:
        return "k_ok: not a boolean"
    if kind == "flows":
        fs = row["fs"]
        if type(fs) is not list or not 0 < len(fs) <= 1024 or not all(_is_int(f, 10_000_000) for f in fs):
            return "fs: not a list of 1 to 1024 function indices"
        return "fs: repeated function index" if len(set(fs)) != len(fs) else None
    if not _is_int(row["f"], 10_000_000):
        return "f: not a function index"
    if not _is_count(row["calls"]):
        return "calls: not a count"
    if kind == "rows":
        if not _is_text(row["p"], 256) or not _is_text(row["s"], 512):
            return "p/s: not a parameter name and a shape key"
        callers = row.get("callers")
        if callers is not None and (
            type(callers) is not dict
            or len(callers) > 16
            or not all(type(c) is str and _CALLER_RE.match(c) and _is_count(w) for c, w in callers.items())
        ):
            return "callers: at most 16 hashed callers with counts"
        return None
    vector = row["v"]
    if type(vector) is not list or len(vector) > 256:
        return "v: not a list of at most 256 pairs"
    for pair in vector:
        if (
            type(pair) is not list
            or len(pair) != 2
            or not _is_text(pair[0], 256)
            or not _is_text(pair[1], 512)
        ):
            return "v: each item is [parameter, shape key]"
    return None


def _row_problems(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for kind in _ROW_KINDS:
        rows = payload.get(kind)
        if rows is None:
            continue
        if type(rows) is not list or len(rows) > MAX_ROWS:
            problems.append(f"{kind}: not a list of at most {MAX_ROWS} rows")
            continue
        for i, row in enumerate(rows):
            problem = _row_problem(kind, row)
            if problem is not None:
                problems.append(f"{kind}/{i}/{problem}")
                if len(problems) >= 20:
                    return problems
    return problems


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def payload_sha256(payload: Mapping[str, Any]) -> str:
    """The identity of a window: sha256 of its canonical JSON (sorted keys, no whitespace)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


# -- function identity -----------------------------------------------------------------------------


def _dotted_names(path: str) -> list[str]:
    """Every module name ``path`` can be imported as, longest first (``src/a/b.py``: ``src.a.b``, ``a.b``, ``b``)."""
    parts = path.removesuffix(".py").split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts)) if parts[i:]]


def _callables(regions: Iterable[FunctionRegion]) -> list[FunctionRegion]:
    return [r for r in regions if r.kind in ("function", "method")]


class FunctionResolver:
    """An index of a run's callable regions by ``(path, qualname)`` and ``(module name, qualname)``.

    Building it walks every region and every dotted name its file can be imported as, which is the
    expensive part (tens of milliseconds for 20,000 regions); a lookup is two dict probes. Build one
    per run and resolve every function through it: rebuilding per function made reading windows
    that were imported before any run existed O(functions x regions) (74 s for 2,000 functions).
    """

    __slots__ = ("_by_module", "_by_path")

    def __init__(self, regions: Iterable[FunctionRegion]) -> None:
        self._by_path: dict[tuple[str, str], list[FunctionRegion]] = {}
        self._by_module: dict[tuple[str, str], list[FunctionRegion]] = {}
        for region in _callables(regions):
            self._by_path.setdefault((region.path, region.qualname), []).append(region)
            for name in _dotted_names(region.path):
                self._by_module.setdefault((name, region.qualname), []).append(region)

    def resolve(self, func: Mapping[str, Any]) -> FunctionRegion | None:
        """The region of one payload function (``module``, ``qualname``, ``line``, ``path``), or None."""
        qualname, line = func.get("qualname"), func.get("line") or 0
        candidates: list[FunctionRegion] = []
        if func.get("path"):
            candidates = self._by_path.get((func["path"], qualname), [])
        if not candidates:
            candidates = self._by_module.get((func.get("module"), qualname), [])
        if len({c.path for c in candidates}) != 1:
            return None  # unknown, or the module name fits files in several places
        return min(candidates, key=lambda c: (abs(c.def_line - line), c.ordinal))


def resolve_functions(
    regions: Sequence[FunctionRegion], funcs: Sequence[Mapping[str, Any]]
) -> list[FunctionRegion | None]:
    """The region of each payload function (``module``, ``qualname``, ``line``, ``path``), or None.

    One :class:`FunctionResolver` for the whole batch; callers resolving functions one at a time
    should keep a resolver instead of calling this per function.
    """
    resolver = FunctionResolver(regions)
    return [resolver.resolve(func) for func in funcs]


def caller_index(regions: Sequence[FunctionRegion]) -> dict[str, FunctionRegion]:
    """Probe caller hash -> region, over every module name each region's file can be imported as."""
    index: dict[str, FunctionRegion] = {}
    for region in _callables(regions):
        for name in _dotted_names(region.path):
            index.setdefault(caller_hash(name, region.qualname), region)
    return index


# -- importing --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ImportResult:
    """What ``coverset flows import`` did with one file."""

    windows: int = 0
    duplicates: int = 0
    rejected: list[tuple[int, list[str]]] = field(default_factory=list)
    """``(line number, problems)`` of every window that was refused."""
    rows: int = 0
    funcs: int = 0
    unresolved: int = 0
    """Functions no region of the reference run matched (kept, resolved again at read time)."""
    services: set[str] = field(default_factory=set)
    batches: list[Path] = field(default_factory=list)


def prod_dir(store: FactsStore, repo_id: int) -> Path:
    """``<facts>/repo=<id>/prod``: every production batch of one repo."""
    return store.repo_dir(repo_id) / PROD_DIR


@contextmanager
def _prod_lock(store: FactsStore, repo_id: int) -> Iterator[None]:
    root = prod_dir(store, repo_id)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _batch_dirs(store: FactsStore, repo_id: int, service: str | None = None) -> list[Path]:
    root = prod_dir(store, repo_id)
    store.pull_batches(root, marker="prod_windows.parquet")  # whole batches from the store, cached
    if not root.is_dir():
        return []
    found: list[Path] = []
    for service_dir in sorted(root.iterdir()):
        name = service_dir.name
        if not name.startswith("service=") or not service_dir.is_dir():
            continue
        if service is not None and name != f"service={service}":
            continue
        found += [b for b in sorted(service_dir.iterdir()) if _BATCH_RE.match(b.name) and b.is_dir()]
    return found


def _known_payloads(store: FactsStore, repo_id: int, service: str) -> set[str]:
    files = [b / "prod_windows.parquet" for b in _batch_dirs(store, repo_id, service)]
    files = [f for f in files if f.exists()]
    if not files:
        return set()
    table = ds.dataset(files, format="parquet").to_table(columns=["payload_sha256"])
    return set(table.column("payload_sha256").to_pylist())


def import_payloads(
    store: FactsStore,
    repo_id: int,
    payloads: Iterable[tuple[int, dict[str, Any] | None, str | None]],
    *,
    regions: Sequence[FunctionRegion] = (),
    service: str | None = None,
    now: datetime | None = None,
) -> ImportResult:
    """Validate and store the windows of one flows file (see the module docstring).

    ``payloads`` is :func:`read_payload_file`'s output. ``regions`` (the reference run's functions)
    resolve ``func_key`` at import; ``service`` overrides every payload's service name.
    """
    result = ImportResult()
    if service is not None and not _SERVICE_RE.match(service):
        raise ValueError(f"invalid service name {service!r} (letters, digits, '.', '_', '-'; 128 at most)")
    imported_at = now or datetime.now(UTC)
    accepted: dict[str, list[tuple[dict[str, Any], str]]] = {}
    seen: set[str] = set()
    for number, payload, problem in payloads:
        if payload is None:
            result.rejected.append((number, [problem or "unreadable"]))
            continue
        if service is not None:
            payload["service"] = service
        problems = validate_payload(payload)
        if problems:
            result.rejected.append((number, problems))
            continue
        sha = payload_sha256(payload)
        if sha in seen:
            result.duplicates += 1
            continue
        seen.add(sha)
        accepted.setdefault(payload["service"], []).append((payload, sha))
    resolver = FunctionResolver(regions) if regions and accepted else None  # once per file, not per window
    with _prod_lock(store, repo_id):
        for svc, windows in sorted(accepted.items()):
            known = _known_payloads(store, repo_id, svc)
            fresh = [(p, sha) for p, sha in windows if sha not in known]
            result.duplicates += len(windows) - len(fresh)
            if not fresh:
                continue
            batch = _write_batch(store, repo_id, svc, fresh, resolver, imported_at, result)
            result.batches.append(batch)
            result.services.add(svc)
    return result


def _k_ok(row: Mapping[str, Any], traces: int | None, gated: bool, k: int) -> bool:
    """Whether a row may be shown: an explicit ``k_ok`` wins; a gating producer's rows passed its
    lifetime gate; anything else must show ``k`` distinct traces within its own window."""
    if "k_ok" in row:
        return bool(row["k_ok"])
    return gated or (traces is not None and traces >= k)


def _write_batch(
    store: FactsStore,
    repo_id: int,
    service: str,
    windows: list[tuple[dict[str, Any], str]],
    resolver: FunctionResolver | None,
    imported_at: datetime,
    result: ImportResult,
) -> Path:
    keys = {row["s"] for p, _ in windows for row in p["rows"]}
    ids = store.extend_dictionary(repo_id, shapes=keys)
    tables: dict[str, list[dict[str, Any]]] = {name: [] for name in PROD_TABLES}
    for payload, sha in windows:
        window = {
            "window_start": _parse_time(payload["window"]["start"]),
            "window_end": _parse_time(payload["window"]["end"]),
            "service": service,
            "env": payload["env"],
        }
        probe = payload.get("probe") or {}
        gated = isinstance(probe.get("k"), int) and not isinstance(probe.get("k"), bool)
        k = probe["k"] if gated else DEFAULT_K
        tables["prod_windows"].append(
            {
                **window,
                "release": payload.get("release"),
                "payload_sha256": sha,
                "imported_at": imported_at,
                "k": k,
                "sample_rate": probe.get("sample_rate")
                if isinstance(probe.get("sample_rate"), int | float)
                else None,
                "probe_json": json.dumps(probe, sort_keys=True)[:65536],
            }
        )
        resolved = [resolver.resolve(f) if resolver else None for f in payload["funcs"]]
        func_cols = []
        for func, region in zip(payload["funcs"], resolved, strict=True):
            func_cols.append(
                {
                    "func_key": region.func_key if region is not None else None,
                    "module": func["module"],
                    "qualname": func["qualname"],
                    "path": func.get("path") or (region.path if region is not None else None),
                    "line": func.get("line"),
                }
            )
            if region is None:
                result.unresolved += 1
        result.funcs += len(func_cols)
        for func, cols in zip(payload["funcs"], func_cols, strict=True):
            tables["prod_funcs"].append(
                {
                    **window,
                    **cols,
                    "calls": func.get("calls"),
                    "sampled": func.get("sampled"),
                    "capped": func.get("capped"),
                }
            )
        for row in payload["rows"]:
            traces = row.get("traces")
            tables["prod_shapes"].append(
                {
                    **window,
                    **func_cols[row["f"]],
                    "param": row["p"],
                    "shape_id": ids.shape_id(row["s"]),
                    "calls": float(row["calls"]),
                    "traces": traces,
                    "k_ok": _k_ok(row, traces, gated, k),
                    "callers_json": json.dumps(row["callers"], sort_keys=True)
                    if row.get("callers")
                    else None,
                }
            )
        for row in payload.get("calls") or ():
            traces = row.get("traces")
            tables["prod_calls"].append(
                {
                    **window,
                    **func_cols[row["f"]],
                    "vector_json": json.dumps([list(pair) for pair in row["v"]]),
                    "calls": float(row["calls"]),
                    "traces": traces,
                    "k_ok": _k_ok(row, traces, gated, k),
                }
            )
        for flow in payload.get("flows") or ():
            traces = flow.get("traces")
            names = sorted(
                func_cols[f]["func_key"] or f"{func_cols[f]['module']}:{func_cols[f]['qualname']}"
                for f in flow["fs"]
            )
            tables["prod_flows"].append(
                {
                    **window,
                    "funcs_json": json.dumps(names),
                    "traces": traces,
                    "k_ok": _k_ok(flow, traces, gated, k),
                }
            )
        result.rows += len(payload["rows"])
        result.windows += 1
    digest = hashlib.sha256("".join(sha for _, sha in windows).encode()).hexdigest()[:12]
    service_dir = prod_dir(store, repo_id) / f"service={service}"
    service_dir.mkdir(parents=True, exist_ok=True)
    final = service_dir / f"batch={imported_at.strftime('%Y%m%dT%H%M%S%fZ')}-{digest}"
    staging = service_dir / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name, rows in tables.items():
            table = pa.Table.from_pylist(rows, schema=PROD_SCHEMAS[name])
            pq.write_table(table, staging / f"{name}.parquet", compression="zstd", use_dictionary=True)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    store.push_tree(final, last="prod_windows.parquet")  # to the store: the marker table goes last
    return final


# -- reading back -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """One imported window."""

    service: str
    env: str
    release: str | None
    start: datetime
    end: datetime
    k: int
    sample_rate: float | None
    batch: Path


def _read(files: Sequence[Path], name: str, starts: Sequence[datetime] | None = None) -> pa.Table:
    present = [f for f in files if f.exists()]
    if not present:
        return PROD_SCHEMAS[name].empty_table()
    dataset = ds.dataset(present, format="parquet", schema=PROD_SCHEMAS[name])
    if starts is None:
        return dataset.to_table()
    return dataset.to_table(
        filter=ds.field("window_start").isin(pa.array(starts, type=pa.timestamp("us", tz="UTC")))
    )


def list_windows(store: FactsStore, repo_id: int, service: str | None = None) -> list[WindowInfo]:
    """Every imported window of the repo (or of one service), oldest first."""
    windows: list[WindowInfo] = []
    for batch in _batch_dirs(store, repo_id, service):
        for row in _read([batch / "prod_windows.parquet"], "prod_windows").to_pylist():
            windows.append(
                WindowInfo(
                    service=row["service"],
                    env=row["env"],
                    release=row["release"],
                    start=row["window_start"],
                    end=row["window_end"],
                    k=row["k"] or 1,
                    sample_rate=row["sample_rate"],
                    batch=batch,
                )
            )
    windows.sort(key=lambda w: (w.end, w.start, w.service))
    return windows


@dataclass(frozen=True, slots=True)
class ProdData:
    """Production rows of the selected windows, with shape ids turned back into keys."""

    windows: list[WindowInfo]
    funcs: list[dict[str, Any]]
    shapes: list[dict[str, Any]]
    """``prod_shapes`` rows plus ``shape_key``."""
    calls: list[dict[str, Any]]
    """``prod_calls`` rows plus ``vector`` (a list of ``(param, shape_key)``)."""
    flows: list[dict[str, Any]]
    """``prod_flows`` rows plus ``funcs`` (func keys, or ``module:qualname`` when unresolved)."""


def _window_length(windows: Sequence[WindowInfo]) -> timedelta:
    """The service's typical window length: the median of its windows' positive durations.

    The median, not the newest, because a process's last window is cut short at exit.
    """
    lengths = sorted(w.end - w.start for w in windows if w.end > w.start)
    return lengths[len(lengths) // 2] if lengths else timedelta(0)


def select_windows(
    windows: Sequence[WindowInfo], *, last_windows: int | None = 1, since: datetime | None = None
) -> list[WindowInfo]:
    """The windows of one service a report pools: every process's, over a span of time.

    Every probe process (each gunicorn or uWSGI worker) exports its own windows, so "the newest N
    windows" by count would pick whichever worker flushed last and analyse a fraction of the
    traffic that changes from one invocation to the next. ``last_windows=N`` therefore means the
    last N window *lengths*: every window that ended within ``N x`` the service's typical window
    length (:func:`_window_length`) of its newest window's end, whichever process wrote it. With
    a single process and full-length windows that is exactly its newest N windows (a last window
    cut short at exit adds one). ``since`` instead takes every window that ended after that time;
    None for both takes everything.
    """
    if not windows:
        return []
    if since is not None:
        cutoff = since if since.tzinfo else since.replace(tzinfo=UTC)
        return [w for w in windows if w.end > cutoff]
    if last_windows is None:
        return list(windows)
    newest = max(w.end for w in windows)
    length = _window_length(windows)
    if length <= timedelta(0):  # only zero-length windows: fall back to counting
        return sorted(windows, key=lambda w: (w.end, w.start))[-last_windows:]
    cutoff = newest - length * last_windows
    return [w for w in windows if w.end > cutoff]


def read_prod(
    store: FactsStore,
    repo_id: int,
    *,
    service: str | None = None,
    last_windows: int | None = 1,
    since: datetime | None = None,
) -> ProdData:
    """The rows of each service's windows chosen by :func:`select_windows` (every window when both
    ``last_windows`` and ``since`` are None)."""
    by_service: dict[str, list[WindowInfo]] = {}
    for window in list_windows(store, repo_id, service):
        by_service.setdefault(window.service, []).append(window)
    chosen: list[WindowInfo] = []
    for windows in by_service.values():
        chosen += select_windows(windows, last_windows=last_windows, since=since)
    chosen.sort(key=lambda w: (w.end, w.service))
    if not chosen:
        return ProdData([], [], [], [], [])
    batches = sorted({w.batch for w in chosen})
    wanted = {(w.service, w.start) for w in chosen}
    starts = sorted({w.start for w in chosen})

    def rows(name: str) -> list[dict[str, Any]]:
        table = _read([b / f"{name}.parquet" for b in batches], name, starts)
        return [r for r in table.to_pylist() if (r["service"], r["window_start"]) in wanted]

    ids = store.dictionary(repo_id)
    shapes = rows("prod_shapes")
    for row in shapes:
        row["shape_key"] = ids.shape_key(row["shape_id"])
    calls = rows("prod_calls")
    for row in calls:
        row["vector"] = [tuple(pair) for pair in json.loads(row["vector_json"])]
    flows = rows("prod_flows")
    for row in flows:
        row["funcs"] = json.loads(row["funcs_json"])
    return ProdData(chosen, rows("prod_funcs"), shapes, calls, flows)


def prune_windows(
    store: FactsStore, repo_id: int, *, older_than: datetime, dry_run: bool = False
) -> list[Path]:
    """Delete every batch whose newest window ended before ``older_than``; returns the batches."""
    cutoff = older_than if older_than.tzinfo else older_than.replace(tzinfo=UTC)
    doomed: list[Path] = []
    with _prod_lock(store, repo_id):
        for batch in _batch_dirs(store, repo_id):
            table = _read([batch / "prod_windows.parquet"], "prod_windows")
            ends = table.column("window_end").to_pylist()
            if ends and max(ends) < cutoff:
                doomed.append(batch)
                if not dry_run:
                    store.remove_tree(batch)  # objects first, then the (hidden-renamed) directory
    return doomed


_RUN_SHAPES_SQL = """
SELECT func_key, param, shape_id, list_sort(list_distinct(flatten(list(test_ids)))) AS test_ids,
       sum(samples)::BIGINT AS samples
FROM call_shapes WHERE func_key IN (SELECT unnest($keys))
GROUP BY func_key, param, shape_id
ORDER BY func_key, param, shape_id
"""


def run_shapes_by_function(
    store: FactsStore, repo_id: int, run_id: str, func_keys: Iterable[str]
) -> dict[str, list[tuple[str, str, tuple[str, ...]]]]:
    """``(param, shape key, nodeids)`` the run's tests passed to each of ``func_keys``.

    Every caller (a test or an in-repo function) is pooled: what matters here is which kinds of
    value reached the function while some test ran. A function no test reached has no entry.
    """
    keys = sorted(set(func_keys))
    if not keys:
        return {}
    con = store.connect(repo_id=repo_id, run_id=run_id, tables=("call_shapes",))
    try:
        rows = con.execute(_RUN_SHAPES_SQL, {"keys": keys}).fetchall()
    finally:
        con.close()
    ids = store.dictionary(repo_id)
    result: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {}
    for func_key, param, shape_id, test_ids, _samples in rows:
        nodeids = tuple(ids.nodeid(t) for t in test_ids or () if t != 0)
        result.setdefault(func_key, []).append((param, ids.shape_key(shape_id), nodeids))
    return result


_REACHED_SUMMARY_SQL = """
SELECT DISTINCT func_key FROM func_body WHERE covered > 0 AND func_key IN (SELECT unnest($keys))
"""
_REACHED_LINES_SQL = """
SELECT DISTINCT f.func_key
FROM functions f JOIN test_lines tl
  ON tl.file_id = f.file_id AND tl.line BETWEEN f.body_start AND f.end_line
WHERE tl.test_id <> 0 AND f.func_key IN (SELECT unnest($keys))
"""


def run_functions_reached(store: FactsStore, repo_id: int, run_id: str, func_keys: Iterable[str]) -> set[str]:
    """Which of ``func_keys`` some test of the run executed, by line coverage (not by shapes).

    "Called in production, never by a test" must not depend on the shape tracer: a run made with
    ``--coverset-shapes off``, one whose tracer could not claim a ``sys.monitoring`` tool id, or a
    zero-parameter generator (no argument and no return shape) has no ``call_shapes`` row for a
    function its tests call. Coverage has the answer: the run's ``func_body`` summary (a body line
    covered, the function page's rule) when it is fresh, else any body line a real test (not
    ``<import>``) ran, straight from ``test_lines``.
    """
    from coverset.facts.queries import summary_is_fresh  # queries is heavy; only this path needs it

    keys = sorted(set(func_keys))
    if not keys:
        return set()
    if summary_is_fresh(store, repo_id, run_id, "func_body"):
        sql, tables = _REACHED_SUMMARY_SQL, ("func_body",)
    else:
        sql, tables = _REACHED_LINES_SQL, ("functions", "test_lines")
    con = store.connect(repo_id=repo_id, run_id=run_id, tables=tables)
    try:
        return {key for (key,) in con.execute(sql, {"keys": keys}).fetchall()}
    finally:
        con.close()


def run_has_shapes(store: FactsStore, repo_id: int, run_id: str) -> bool:
    """Whether the run recorded any input shapes (False: shapes were off or the tracer never ran)."""
    con = store.connect(repo_id=repo_id, run_id=run_id, tables=("call_shapes",))
    try:
        return con.execute("SELECT EXISTS (SELECT 1 FROM call_shapes)").fetchone() == (True,)
    finally:
        con.close()


def problems_summary(rejected: Sequence[tuple[int, list[str]]]) -> Counter[str]:
    """How often each first problem occurred across rejected windows (for a one-line report)."""
    return Counter(problems[0].split(":", 1)[0] for _, problems in rejected if problems)
