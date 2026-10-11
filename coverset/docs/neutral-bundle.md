# The neutral bundle: coverset's semantic-coverage model on disk

A **neutral bundle** is what a test run leaves behind for coverset to ingest, render and reason
about, in a form that does not depend on the language or the test framework: which tests ran and
how they ended, which lines and branches each test executed, where the functions are, and the
shapes of the values that flowed into each function's parameters. `pytest-coverset` writes one for
Python; a Jest, Vitest or Mocha reporter writes the same files for TypeScript. Everything
downstream (the facts store, the input-class catalogue, the pages, the sticky comment, the static
report) reads only this model, through `coverset_model` (`packages/coverset-model`, no
dependencies, Python 3.12+).

This page is the specification. `coverset bundle validate DIR` checks a bundle against it, and
`docs/schema/` holds the JSON Schemas it enforces. Version 1.

## Layout

A bundle is a directory (or a `tar.gz` of one, flat members) with these files. Every `.jsonl`
file may instead be `.jsonl.gz`. Encoding is UTF-8; JSON strings never contain lone surrogates.

| File | Required | Rows |
|---|---|---|
| `manifest.json` | yes | one object (below) |
| `tests.jsonl` | yes | one per collected test |
| `files.jsonl` | yes | one per measured source file (static facts) |
| `coverage.jsonl` | yes | one per (test, file) with executed lines |
| `arcs.jsonl` | no | one per file: executed branch arcs over the whole run |
| `functions.jsonl` | depends | one per function region; required unless the language adapter derives regions from sources (Python does) |
| `shapes.jsonl` | no | observation and sampling rows of the shape tracer |
| `links.jsonl` | no | `{"test", "invariant"}` pairs from `@pytest.mark.invariant`-style markers |

Paths are POSIX, relative to the repository root, never absolute, never containing `..` or a
drive letter, at most 512 characters. Test ids are the framework's own (pytest nodeids, Jest's
`file > describe > name`), at most 512 characters, unique within the bundle. Line numbers are
1-based. A bundle is untrusted input: readers apply the limits in `docs/security.md` section 7
(file sizes, line lengths, row counts, gzip ratios) and refuse anything outside them.

### `manifest.json`

```json
{"format": "coverset-neutral", "version": 1, "language": "python",
 "producer": {"name": "pytest-coverset", "version": "0.1.0"},
 "created_at": "2026-10-11T19:04:05Z", "shard": 0,
 "meta": {"tracer": "CTracer", "tool_id": 4, "python": "3.13.0"}}
```

`format` and `version` are checked first; an unknown major version is refused. `language` is a
lowercase identifier (`python`, `typescript`, `javascript`, ...). `shard` is the shard index when
the run was split, else `null`. `meta` is free-form producer detail that is stored verbatim in
the run's ingest record (the Python plugin puts its tracer, tool id, warnings and caps there).

### `tests.jsonl`

```json
{"id": "tests/test_refund.py::test_refund_partial", "file": "tests/test_refund.py", "line": 12,
 "outcome": "failed", "duration_s": 0.013, "failed_phase": "call", "fail_sig": "3f9a1c0e2b7d4411",
 "message": "assert -98 == -100", "markers": ["invariant"], "invariants": ["cents_conserved"],
 "property": false, "start": 1760209445.1, "stop": 1760209445.11}
```

`outcome` is one of `passed`, `failed`, `error`, `skipped`, `xfailed`, `xpassed`, or `null` for a
test that was collected but never ran (cancelled shard, stop file). `failed_phase` is `setup`,
`call`, `teardown` or `null`. `fail_sig` is a stable hash of the failure's exception type and the
crash location (never of the message), so the same failure has the same signature across runs.
`message` is the failure text, at most 8 KiB. `property` marks property-based tests (Hypothesis,
fast-check). `markers` and `invariants` may be empty.

### `files.jsonl`

```json
{"path": "src/ledger/billing/allocate.py", "n_lines": 41, "statements": [1, 3, 5, 8, 9, 10],
 "excluded": [], "branches": [[8, 9], [8, 12]]}
```

`statements` are the lines that can execute (coverage.py's statements; Istanbul's statement map
first lines), `excluded` the ones the producer excluded from measurement, `branches` the possible
branch arcs as `[from, to]` line pairs (coverage.py's arcs; for Istanbul, each branch location to
each target's first line). A file with no statements is still listed so its functions have a home.

### `coverage.jsonl`

```json
{"test": "tests/test_refund.py::test_refund_partial", "file": "src/ledger/billing/allocate.py",
 "lines": [8, 9, 10, 14]}
{"test": "<import>", "file": "src/ledger/billing/allocate.py", "lines": [1, 3, 5]}
```

One row per (test, file) with the sorted executed lines; `<import>` is the reserved test id for
code that ran outside any test (module import, collection). Lines must be statements of that
file (readers drop the rest). Optional `"arcs": [[from, to], ...]` per row holds the test's
executed branch arcs.

### `arcs.jsonl`

```json
{"file": "src/ledger/billing/allocate.py", "arcs": [[8, 9], [8, 12], [9, 10]]}
```

The run-wide executed arcs per file (the union over tests). A branch line whose possible arcs
are not all here is partial.

### `functions.jsonl`

```json
{"key": "7c1d2a9e5b304f11", "path": "src/ledger/billing/allocate.py", "qualname": "allocate_cents",
 "ordinal": 0, "def_line": 8, "body_start": 10, "end_line": 24, "body_hash": "a1b2c3d4",
 "sig_hash": "e5f60718", "params": ["total_cents", "weights"], "kind": "function",
 "types": {"total_cents": {"kind": "int"}, "weights": {"kind": "list", "elem": {"kind": "float"}},
           "return": {"kind": "list", "elem": {"kind": "int"}}}}
```

`key` is `blake2b(path + "\0" + qualname + "\0" + ordinal, digest_size=8)` in hex; `ordinal`
numbers same-named regions in one file in source order (overloads, redefinitions). `def_line` is
the first line of the declaration (decorators included), `body_start` the first body statement
after a leading docstring, `end_line` the last line. `body_hash` and `sig_hash` are the
producer's stable hashes of the body and the signature (formatting-insensitive where the language
allows); readers compare them only with hashes from the same producer. `kind` is `function`,
`method`, `class` or `module`. `types` maps parameter names (and `return`) to **type
descriptors** (below) or `null` when unannotated; it may be omitted.

### `shapes.jsonl`

```json
{"kind": "obs", "func": ["src/ledger/billing/allocate.py", "allocate_cents", 8], "param": "weights",
 "shape": "list|elem:float|len:2-10|order:unsorted", "caller": {"test": "tests/test_allocate.py::test_split"},
 "samples": 3, "tests": ["tests/test_allocate.py::test_split"], "truncated": false,
 "example": "[1.0, 2.0, 1.0]", "example_test": "tests/test_allocate.py::test_split"}
{"kind": "obs", "func": ["src/ledger/billing/allocate.py", "allocate_cents", 8], "param": "return",
 "shape": "list|elem:int|len:2-10", "caller": ["src/ledger/billing/refund.py", "refund", 6],
 "samples": 1, "tests": ["tests/test_refund.py::test_refund_partial"], "truncated": false,
 "example": null, "example_test": null}
{"kind": "sampling", "func": ["src/ledger/billing/allocate.py", "allocate_cents", 8], "calls": 12,
 "tests": ["tests/test_allocate.py::test_split", "tests/test_refund.py::test_refund_partial"],
 "truncated": false, "cap": null}
```

`func` is `[path, qualname, first line]` (the declaration's first line, so it matches
`functions.jsonl`'s `def_line`). `param` is a parameter name, `return`, `<receiver>` (the
concrete class of `self`/`this`), `*args` or `**kwargs`. `caller` is either `[path, qualname,
line]` for an in-repo caller or `{"test": id}` when the test called the function directly.
`samples` counts calls that produced this shape through this caller; `tests` lists the tests
during which it was seen; `example` is a redacted, shortened value repr (at most 200 characters)
and `example_test` the test it came from. A `sampling` row per traced function gives the total
calls, the tests that were sampled, whether sampling stopped early (`truncated`) and which cap
stopped it (`sample`, `full` or `null`).

### Shape keys

A shape key is `<type>` followed by `|`-joined feature tokens sorted alphabetically, so keys are
canonical. Type tokens are the value vocabulary, not the language's type names: `NoneType`,
`bool`, `int`, `float`, `str`, `bytes`, `list`, `tuple`, `dict`, `set`, `frozenset`, or
`obj:<module>.<qualname>` for anything else. Feature tokens: bool `true|false`; int
`neg|zero|pos`, `big` (beyond ±2³¹); float `nan|inf|inf-neg|neg|zero|pos`, `integral`; str
`len:<0|1|2-10|11-100|101-1000|>1000>`, `empty|whitespace|ascii|unicode`, `ws-edge`, `digits`,
`kind:<email/<plain|multi-label-domain|plus-tag|uppercase|idn>|quantity/<single-unit|multi-unit|multi-letter-unit|decimal|signed|trailing-garbage>|numeric|iso-date|url>`;
bytes `len:<bucket>`; containers `len:<bucket>`, `elem:<type or mixed>`, `key:<type or mixed>`,
`order:<sorted|reverse|unsorted|n/a>`, `dups`, `any_zero|any_neg|any_nan|any_inf|all_zero|sum_zero`,
`pair:<lt|eq|gt|mixed>`; objects `dataclass`, `pydantic`. `coverset_model.shape_keys` parses and
validates keys; `docs/input-classes.md` explains how input classes are derived from them.

A TypeScript reporter maps JavaScript values onto this vocabulary: integral numbers → `int`,
other numbers → `float` (`NaN` → `float|nan`), strings → `str`, booleans → `bool`, `null` and
`undefined` → `NoneType`, arrays → `list`, plain objects and `Map` → `dict`, `Set` → `set`,
`Uint8Array`/`Buffer` → `bytes`, class instances → `obj:<module>.<ClassName>`.

### Type descriptors

Expected input classes come from the declared type of a parameter when there is one. The
descriptor is a small JSON tree, the same for every language:

| Descriptor | Meaning |
|---|---|
| `{"kind": "int"}`, `"float"`, `"str"`, `"bool"`, `"bytes"`, `"none"` | a scalar type |
| `{"kind": "list", "elem": D}`, `{"kind": "set", "elem": D}`, `{"kind": "tuple", "elems": [D, ...]}` | containers |
| `{"kind": "dict", "key": D, "value": D}` | mappings |
| `{"kind": "union", "arms": [D, ...]}` | any of the arms (`Optional[X]` is a union with `none`) |
| `{"kind": "literal", "values": [v, ...]}` | one of the listed JSON values |
| `{"kind": "enum", "name": "Role", "members": ["admin", "member"]}` | a closed enumeration |
| `{"kind": "object", "name": "ledger.util.money.Money"}` | a class or interface |
| `{"kind": "any"}` | unknown or unannotated |

`coverset_model.python_types.descriptor(annotation_text)` converts a Python annotation to a
descriptor (`int | None` → union of `int` and `none`; `Literal["a", "b"]` → literal; an Enum
subclass resolved from the sources → enum). A TypeScript reporter does the same from the
TypeScript type (`number` → `float` unless the declared values are integral, `string` → `str`,
`T | undefined` → union with `none`, string-literal unions → literal, `enum` → enum, interfaces
and classes → object). `docs/input-classes.md` lists which classes each descriptor expects.

## Reading a bundle

`coverset_model.io.read_bundle(path)` returns a `NeutralBundle` (the same in-memory contract the
facts store already consumes), after validating the manifest and every row; `validate(path)`
returns the problems without building anything. The Python adapter's own on-disk bundle
(`coverage.sqlite`, `coverage.json`, `shapes.jsonl.gz`, `results.jsonl`, ...) is converted to the
same `NeutralBundle` by `coverset.facts.convert.read_python_bundle`; `pytest-coverset` can also
write the neutral files directly (`--coverset-neutral`). A bundle whose `manifest.json` says
`coverset-neutral` is read by the neutral adapter whatever its language; the orchestrator picks
the reader by the manifest.

## What a producer must get right

* One test id per test, stable across runs, and the same id in `tests.jsonl`, `coverage.jsonl`
  and `shapes.jsonl`.
* Executed lines are per test, with everything outside a test under `<import>`; setup and
  teardown belong to the test.
* `files.jsonl` lists every measured file, with statements, so patch coverage can tell
  "not executed" from "not measurable".
* Function regions with body ranges, so "test T calls function F" (T's lines intersect F's body)
  holds; a decorator or signature change counts only when a body line is covered.
* Shape keys built from the vocabulary above, sampled per function with the cap recorded, with
  examples redacted at the source: a bundle is attacker-controlled on a pull request, and so is
  every string in it.
