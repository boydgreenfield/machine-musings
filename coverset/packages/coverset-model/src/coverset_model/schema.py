"""JSON Schemas of the neutral bundle's files, and a small validator for the subset they use.

``SCHEMAS`` maps a file stem (``manifest``, ``tests``, ``files``, ``coverage``, ``arcs``,
``functions``, ``shapes``, ``links``) to its schema (draft 2020-12). The ``.jsonl`` schemas
describe one row. ``docs/schema/<stem>.schema.json`` is :func:`dump` of each, written by
``uv run python -m coverset_model schemas docs/schema`` and checked by a test.

The schemas carry the structure (types, required fields, enums, bounds, patterns); the reader
(:mod:`coverset_model.io`) adds what JSON Schema cannot say: path rules, unique test ids, lines
within a file's statements, function keys, shape-key grammar.

:func:`check` validates a value against a schema without any dependency. It supports ``type``
(including a list of types), ``enum``, ``const``, ``required``, ``properties``,
``additionalProperties`` (a boolean or a schema), ``items``, ``prefixItems``, ``minItems``/``maxItems``,
``minLength``/``maxLength``, ``minimum``/``maximum``, ``pattern``, ``anyOf``, local ``$ref``
into ``$defs`` and the boolean schemas ``true`` and ``false``. It never recurses deeper than
:data:`MAX_NESTING` levels into a value (deeper is one violation, not a ``RecursionError``).

A type descriptor may nest at most ``coverset_model.types.MAX_DEPTH`` (32) levels below the root.
JSON Schema has no depth keyword, so the schema unrolls the recursion: ``$defs/descriptor`` (the
root) refers to ``descriptor_1`` for its children, and so on down to ``descriptor_32``, whose
children are refused (``false``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = ["SCHEMAS", "SchemaError", "check", "dump", "iter_violations", "row_schema", "write_all"]

FORMAT: Final = "coverset-neutral"
VERSION: Final = 1
_BASE: Final = "https://coverset.dev/schema/neutral/v1/"
MAX_ID: Final = 512
MAX_PATH: Final = 512
MAX_NAME: Final = 512
MAX_LINE: Final = 1_000_000
MAX_NESTING: Final = 64
"""Levels :func:`check` descends into a value before reporting it as nested too deep."""
DESCRIPTOR_DEPTH: Final = 32
"""Deepest level of a type descriptor (the root is level 0), as ``coverset_model.types.MAX_DEPTH``."""
CREATED_AT_PATTERN: Final = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$"
)
"""ISO 8601 / RFC 3339 date-time: ``2026-10-11T19:04:05Z``, ``2026-10-11T19:04:05.123+02:00``."""

_ID: Final = {"type": "string", "minLength": 1, "maxLength": MAX_ID}
_PATH: Final = {"type": "string", "minLength": 1, "maxLength": MAX_PATH}
_NAME: Final = {"type": "string", "minLength": 1, "maxLength": MAX_NAME}
_LINE: Final = {"type": "integer", "minimum": 1, "maximum": MAX_LINE}
_LINES: Final = {"type": "array", "items": _LINE}
_ARC: Final = {
    "type": "array",
    "items": {"type": "integer", "minimum": -MAX_LINE, "maximum": MAX_LINE},
    "minItems": 2,
    "maxItems": 2,
}
_ARCS: Final = {"type": "array", "items": _ARC}
_FUNC: Final = {
    "description": "[path, qualname, first line of the declaration]",
    "type": "array",
    "prefixItems": [_PATH, _NAME, _LINE],
    "minItems": 3,
    "maxItems": 3,
}
_NUMBER_OR_NULL: Final = {"type": ["number", "null"]}

_DESCRIPTOR_BODY: Final[dict[str, Any]] = {
    "description": (
        "The fields of every type descriptor (docs/neutral-bundle.md, 'Type descriptors'); "
        "the descriptor_<n> definitions bound how deep children nest."
    ),
    "type": "object",
    "required": ["kind"],
    "anyOf": [{"properties": {"elem": False}}, {"properties": {"elems": False}}],
    "properties": {
        "kind": {
            "enum": [
                "int",
                "float",
                "str",
                "bool",
                "bytes",
                "none",
                "list",
                "set",
                "frozenset",
                "tuple",
                "dict",
                "union",
                "literal",
                "enum",
                "object",
                "any",
            ]
        },  # fmt: skip
        "elem": {"type": "object"},
        "elems": {"type": "array", "maxItems": 256},
        "key": {"type": "object"},
        "value": {"type": "object"},
        "arms": {"type": "array", "minItems": 1, "maxItems": 256},
        "values": {
            "type": "array",
            "items": {"type": ["string", "integer", "number", "boolean", "null"]},
            "maxItems": 256,
        },
        "name": _NAME,
        "members": {"type": "array", "items": _NAME, "minItems": 1, "maxItems": 256},
    },
    "additionalProperties": False,
}


def _descriptor_level(level: int) -> dict[str, Any]:
    """The descriptor at ``level`` (0 = root): the body, children one level down or none at the last."""
    if level >= DESCRIPTOR_DEPTH:
        child: Any = False
        return {
            "$ref": "#/$defs/descriptor_body",
            "properties": {
                "elem": child,
                "elems": {"maxItems": 0},
                "key": child,
                "value": child,
                "arms": child,
            },
        }
    ref = {"$ref": f"#/$defs/descriptor_{level + 1}"}
    return {
        "$ref": "#/$defs/descriptor_body",
        "properties": {
            "elem": ref,
            "elems": {"items": ref},
            "key": ref,
            "value": ref,
            "arms": {"items": ref},
        },
    }


_DESCRIPTOR_DEFS: Final[dict[str, Any]] = {
    "descriptor": {
        "description": "A type descriptor (docs/neutral-bundle.md, 'Type descriptors'), at most 32 levels deep.",
        **_descriptor_level(0),
    },
    **{f"descriptor_{n}": _descriptor_level(n) for n in range(1, DESCRIPTOR_DEPTH + 1)},
    "descriptor_body": _DESCRIPTOR_BODY,
}


def _schema(stem: str, title: str, body: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{_BASE}{stem}.schema.json",
        "title": title,
        **body,
        **extra,
    }


SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "manifest": _schema(
        "manifest",
        "manifest.json: what the bundle is and who wrote it",
        {
            "type": "object",
            "required": ["format", "version", "language", "producer"],
            "properties": {
                "format": {"const": FORMAT},
                "version": {"const": VERSION},
                "language": {"type": "string", "pattern": "^[a-z][a-z0-9_+-]{0,31}$"},
                "producer": {
                    "type": "object",
                    "required": ["name", "version"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 128},
                        "version": {"type": "string", "minLength": 1, "maxLength": 64},
                    },
                    "additionalProperties": False,
                },
                "created_at": {"type": ["string", "null"], "maxLength": 64, "pattern": CREATED_AT_PATTERN},
                "shard": {"type": ["integer", "null"], "minimum": 0, "maximum": 1_000_000},
                "meta": {"type": "object"},
            },
            "additionalProperties": False,
        },
    ),
    "tests": _schema(
        "tests",
        "tests.jsonl: one row per collected test",
        {
            "type": "object",
            "required": ["id", "file", "outcome"],
            "properties": {
                "id": _ID,
                "file": _PATH,
                "line": {"type": ["integer", "null"], "minimum": 1, "maximum": MAX_LINE},
                "outcome": {"enum": ["passed", "failed", "error", "skipped", "xfailed", "xpassed", None]},
                "duration_s": {"type": ["number", "null"], "minimum": 0},
                "failed_phase": {"enum": ["setup", "call", "teardown", None]},
                "fail_sig": {"type": ["string", "null"]},
                "message": {"type": ["string", "null"]},
                "markers": {"type": "array", "items": _NAME},
                "invariants": {"type": "array", "items": _NAME},
                "property": {"type": "boolean"},
                "start": _NUMBER_OR_NULL,
                "stop": _NUMBER_OR_NULL,
            },
            "additionalProperties": False,
        },
    ),
    "files": _schema(
        "files",
        "files.jsonl: static facts of one measured source file",
        {
            "type": "object",
            "required": ["path", "statements"],
            "properties": {
                "path": _PATH,
                "n_lines": {"type": "integer", "minimum": 0, "maximum": MAX_LINE},
                "statements": _LINES,
                "excluded": _LINES,
                "branches": _ARCS,
            },
            "additionalProperties": False,
        },
    ),
    "coverage": _schema(
        "coverage",
        "coverage.jsonl: the lines one test (or <import>) executed in one file",
        {
            "type": "object",
            "required": ["test", "file", "lines"],
            "properties": {"test": _ID, "file": _PATH, "lines": _LINES, "arcs": _ARCS},
            "additionalProperties": False,
        },
    ),
    "arcs": _schema(
        "arcs",
        "arcs.jsonl: the branch arcs executed in one file over the whole run",
        {
            "type": "object",
            "required": ["file", "arcs"],
            "properties": {"file": _PATH, "arcs": _ARCS},
            "additionalProperties": False,
        },
    ),
    "functions": _schema(
        "functions",
        "functions.jsonl: one function region",
        {
            "type": "object",
            "required": [
                "path",
                "qualname",
                "ordinal",
                "def_line",
                "body_start",
                "end_line",
                "body_hash",
                "sig_hash",
                "params",
                "kind",
            ],  # fmt: skip
            "properties": {
                "key": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
                "path": _PATH,
                "qualname": _NAME,
                "ordinal": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
                "def_line": _LINE,
                "body_start": _LINE,
                "end_line": _LINE,
                "body_hash": {"type": "string", "maxLength": 128},
                "sig_hash": {"type": "string", "maxLength": 128},
                "params": {"type": "array", "items": _NAME, "maxItems": 1024},
                "kind": {"enum": ["function", "method", "class", "module"]},
                "types": {
                    "type": "object",
                    "additionalProperties": {"anyOf": [{"$ref": "#/$defs/descriptor"}, {"type": "null"}]},
                },
            },
            "additionalProperties": False,
        },
        **{"$defs": _DESCRIPTOR_DEFS},
    ),
    "shapes": _schema(
        "shapes",
        "shapes.jsonl: an observation row or a sampling row of the shape tracer",
        {"anyOf": [{"$ref": "#/$defs/obs"}, {"$ref": "#/$defs/sampling"}]},
        **{
            "$defs": {
                "obs": {
                    "type": "object",
                    "required": ["kind", "func", "param", "shape", "caller", "samples", "tests"],
                    "properties": {
                        "kind": {"const": "obs"},
                        "func": _FUNC,
                        "param": _NAME,
                        "shape": {"type": "string", "minLength": 1, "maxLength": 512},
                        "caller": {
                            "anyOf": [
                                _FUNC,
                                {
                                    "type": "object",
                                    "required": ["test"],
                                    "properties": {"test": _ID},
                                    "additionalProperties": False,
                                },
                            ]
                        },
                        "samples": {"type": "integer", "minimum": 1},
                        "tests": {"type": "array", "items": _ID},
                        "truncated": {"type": "boolean"},
                        "example": {"type": ["string", "null"]},
                        "example_test": {"anyOf": [_ID, {"type": "null"}]},
                    },
                    "additionalProperties": False,
                },
                "sampling": {
                    "type": "object",
                    "required": ["kind", "func", "calls", "tests"],
                    "properties": {
                        "kind": {"const": "sampling"},
                        "func": _FUNC,
                        "calls": {"type": "integer", "minimum": 0},
                        "tests": {"type": "array", "items": _ID},
                        "truncated": {"type": "boolean"},
                        "cap": {"enum": ["sample", "full", None]},
                    },
                    "additionalProperties": False,
                },
            }
        },
    ),
    "links": _schema(
        "links",
        "links.jsonl: a test checks an invariant",
        {
            "type": "object",
            "required": ["test", "invariant"],
            "properties": {"test": _ID, "invariant": _NAME},
            "additionalProperties": False,
        },
    ),
}


class SchemaError(ValueError):
    """A schema uses something :func:`check` does not support."""


@dataclass(frozen=True, slots=True)
class Violation:
    """One way a value fails a schema: where (a JSON-pointer-like path) and what."""

    path: str
    message: str
    keyword: str
    """The schema keyword that failed (``additionalProperties`` ones are reported, not fatal)."""


def row_schema(stem: str, row: object) -> dict[str, Any]:
    """The schema one row of ``stem`` is checked against (shapes rows by their ``kind``)."""
    schema = SCHEMAS[stem]
    if stem == "shapes" and isinstance(row, dict) and row.get("kind") in ("obs", "sampling"):
        return {"$defs": schema["$defs"], **schema["$defs"][row["kind"]]}
    return schema


def check(value: object, schema: Mapping[str, Any], *, limit: int = 20) -> list[Violation]:
    """The ways ``value`` fails ``schema`` (at most ``limit``); an empty list means valid."""
    found: list[Violation] = []
    for violation in iter_violations(value, schema):
        found.append(violation)
        if len(found) >= limit:
            break
    return found


def iter_violations(value: object, schema: Mapping[str, Any]) -> Iterator[Violation]:
    """The ways ``value`` fails ``schema``, lazily: stop iterating to stop checking."""
    return _check(value, schema, schema, "")


_JSON_TYPES: Final = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}
_SUPPORTED: Final = frozenset(
    {"$schema", "$id", "$defs", "$ref", "title", "description", "type", "enum", "const", "required",
     "properties", "additionalProperties", "items", "prefixItems", "minItems", "maxItems", "minLength", "maxLength",
     "minimum", "maximum", "pattern", "anyOf"}
)  # fmt: skip
_PATTERNS: dict[str, re.Pattern[str]] = {}


def _resolve(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/$defs/"):
        raise SchemaError(f"only local $defs references are supported: {ref}")
    return root["$defs"][ref.removeprefix("#/$defs/")]


def _same(a: object, b: object) -> bool:
    """JSON equality: ``True`` is not ``1``."""
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


def _check(
    value: Any, schema: Mapping[str, Any] | bool, root: Mapping[str, Any], at: str, depth: int = 0
) -> Iterator[Violation]:
    if schema is True:
        return
    if schema is False:
        yield Violation(at or "/", "not allowed here", "false")
        return
    if depth > MAX_NESTING:
        yield Violation(at or "/", f"nested deeper than {MAX_NESTING} levels", "depth")
        return
    unknown = set(schema) - _SUPPORTED
    if unknown:
        raise SchemaError(f"unsupported schema keywords: {sorted(unknown)}")
    if "$ref" in schema:
        yield from _check(value, _resolve(root, schema["$ref"]), root, at, depth)
    where = at or "/"
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_JSON_TYPES[t](value) for t in types):
            yield Violation(where, f"expected {' or '.join(types)}", "type")
            return
    if "const" in schema and not _same(value, schema["const"]):
        yield Violation(where, f"must be {json.dumps(schema['const'])}", "const")
    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        options = ", ".join(json.dumps(o) for o in schema["enum"])
        yield Violation(where, f"must be one of {options}", "enum")
    if "anyOf" in schema and not any(_valid(value, s, root, at, depth) for s in schema["anyOf"]):
        yield Violation(where, "matches none of the allowed forms", "anyOf")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            yield Violation(where, f"shorter than {schema['minLength']} characters", "minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            yield Violation(where, f"longer than {schema['maxLength']} characters", "maxLength")
        if "pattern" in schema:
            pattern = _PATTERNS.get(schema["pattern"]) or _PATTERNS.setdefault(
                schema["pattern"], re.compile(schema["pattern"])
            )
            if not pattern.search(value):
                yield Violation(where, f"does not match {schema['pattern']}", "pattern")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            yield Violation(where, f"below the minimum {schema['minimum']}", "minimum")
        if "maximum" in schema and value > schema["maximum"]:
            yield Violation(where, f"above the maximum {schema['maximum']}", "maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            yield Violation(where, f"fewer than {schema['minItems']} items", "minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            yield Violation(where, f"more than {schema['maxItems']} items", "maxItems")
        prefix = schema.get("prefixItems", ())
        for index, (item, item_schema) in enumerate(zip(value, prefix, strict=False)):
            yield from _check(item, item_schema, root, f"{at}/{index}", depth + 1)
        if "items" in schema:
            for index, item in enumerate(value[len(prefix) :], start=len(prefix)):
                yield from _check(item, schema["items"], root, f"{at}/{index}", depth + 1)
    if isinstance(value, dict):
        for name in schema.get("required", ()):
            if name not in value:
                yield Violation(where, f"missing required field {name!r}", "required")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                yield from _check(item, properties[name], root, f"{at}/{name}", depth + 1)
            elif extra is False:
                yield Violation(f"{at}/{name}", "unknown field", "additionalProperties")
            elif isinstance(extra, Mapping):
                yield from _check(item, extra, root, f"{at}/{name}", depth + 1)


def _valid(
    value: Any, schema: Mapping[str, Any] | bool, root: Mapping[str, Any], at: str, depth: int
) -> bool:
    return next(_check(value, schema, root, at, depth), None) is None


def dump(stem: str) -> str:
    """The schema of ``stem`` as the text of ``docs/schema/<stem>.schema.json``."""
    return json.dumps(SCHEMAS[stem], indent=2, ensure_ascii=False) + "\n"


def write_all(directory: Path) -> list[Path]:
    """Write every schema to ``directory/<stem>.schema.json``; returns the paths written."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for stem in SCHEMAS:
        path = directory / f"{stem}.schema.json"
        path.write_text(dump(stem), encoding="utf-8")
        written.append(path)
    return written
