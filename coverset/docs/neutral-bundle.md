# The neutral bundle: coverset's semantic-coverage model on disk

A **neutral bundle** is what a test run leaves behind for coverset to ingest, render and reason
about, in a form that does not depend on the language or the test framework: which tests ran and
how they ended, which lines and branches each test executed, where the functions are, and the
shapes of the values that flowed into each function's parameters. `pytest-coverset` writes one for
Python; a Jest, Vitest or Mocha reporter writes the same files for TypeScript. Everything
downstream (the facts store, the input-class catalogue, the pages, the sticky comment, the static
report) reads only this model, through `coverset_model` (`packages/coverset-model`, no
dependencies, Python 3.12+).

This page is the specification. `coverset bundle validate PATH` checks a bundle against it (and
`coverset bundle info PATH` summarises one), and [`docs/schema/`](schema/) holds the JSON Schemas
of every file, generated from `coverset_model.schema` (`uv run python -m coverset_model schemas
docs/schema`; a test fails when they drift). Version 1.

## Layout

A bundle is a directory (or a `tar.gz` of one, flat members: regular files only, no
directories, links or devices) with these files. Every `.jsonl` file may instead be `.jsonl.gz`,
but not both. Encoding is UTF-8; JSON strings never contain lone surrogates, and numbers are
finite (no `NaN` or `Infinity`). Other files in the directory are ignored. Every row is one JSON
object per line; fields not listed in the schemas are reported by `validate` and ignored by
readers (producer-specific detail goes in `manifest.json`'s `meta`).

| File | Required | Rows |
|---|---|---|
| `manifest.json` | yes | one object (below) |
| `tests.jsonl` | yes | one per collected test |
| `files.jsonl` | yes | one per measured source file (static facts) |
| `coverage.jsonl` | yes | one per (test, file) with executed lines |
| `arcs.jsonl` | no | one per file: executed branch arcs over the whole run |
| `functions.jsonl` | depends | one per function region; required unless the language adapter derives regions from sources (Python does): `validate` reports a missing one, and the run then has no regions for those files |
| `shapes.jsonl` | no | observation and sampling rows of the shape tracer |
| `links.jsonl` | no | `{"test", "invariant"}` pairs from `@pytest.mark.invariant`-style markers |

Paths are POSIX, relative to the repository root, never absolute, never containing `.`, `..` or
empty segments, a backslash, a control character or a drive letter, at most 512 characters. Test
ids are the framework's own (pytest nodeids, Jest's `file > describe > name`), at most 4,096
characters (parametrised ids can be long), unique within the bundle. Line numbers are 1-based and at most 1,000,000. A bundle is
untrusted input; see [Reading a bundle](#reading-a-bundle) for the limits readers apply.

### `manifest.json`

```json
{"format": "coverset-neutral", "version": 1, "language": "python",
 "producer": {"name": "pytest-coverset", "version": "0.1.0"},
 "created_at": "2026-10-11T19:04:05Z", "shard": 0,
 "meta": {"tracer": "CTracer", "tool_id": 4, "python": "3.13.0"}}
```

`format` and `version` are checked first; `version` is the integer 1 (the schema says `const 1`)
and any other value is refused. `language` is a lowercase identifier (`python`, `typescript`, `javascript`, ...;
`^[a-z][a-z0-9_+-]{0,31}$`), `producer` names the writer (`name` up to 128 characters, `version`
up to 64). `created_at` is an ISO 8601 date-time (`2026-10-11T19:04:05Z`, optional fractional seconds,
`Z` or a `+HH:MM`/`-HH:MM` offset) or `null`; any other text breaks the schema, and a manifest
that breaks the schema refuses the bundle. `shard` is the shard index when the
run was split, else `null`. `meta` is free-form producer detail that becomes the in-memory
bundle's `meta` (the Python plugin puts its tracer, tool id, warnings and caps there); the keys
`bundle` and `convert` are the reader's own (below) and are never written. `manifest.json` is at
most 1 MiB, and any schema violation in it refuses the bundle.

### `tests.jsonl`

```json
{"id": "tests/test_refund.py::test_refund_partial", "file": "tests/test_refund.py", "line": 12,
 "outcome": "failed", "duration_s": 0.013, "failed_phase": "call", "fail_sig": "3f9a1c0e2b7d4411",
 "message": "assert -98 == -100", "markers": ["invariant"], "invariants": ["cents_conserved"],
 "property": false, "start": 1760209445.1, "stop": 1760209445.11}
```

`id`, `file` and `outcome` are required; every other field may be omitted (`null`, empty or
`false`). `outcome` is one of `passed`, `failed`, `error`, `skipped`, `xfailed`, `xpassed`, or
`null` for a test that was collected but never ran (cancelled shard, stop file). `<import>` is
not a test id. `failed_phase` is `setup`, `call`, `teardown` or `null`. `fail_sig` is a stable
hash of the failure's exception type and the crash location (never of the message), so the same
failure has the same signature across runs; readers cut it at 64 characters. `message` is the
failure text, cut at 8 KiB (8,192 characters). `property` marks property-based tests
(Hypothesis, fast-check). `markers` and `invariants` may be empty. A test id listed twice keeps
its first row.

### `files.jsonl`

```json
{"path": "src/ledger/billing/allocate.py", "n_lines": 41, "statements": [1, 3, 5, 8, 9, 10],
 "excluded": [], "branches": [[8, 9], [8, 12]]}
```

`statements` are the lines that can execute (coverage.py's statements; Istanbul's statement map
first lines), `excluded` the ones the producer excluded from measurement, `branches` the possible
branch arcs as `[from, to]` line pairs (coverage.py's arcs; for Istanbul, each branch location to
each target's first line; a negative target is an exit, as in coverage.py). `path` and
`statements` are required; `n_lines` defaults to the largest line the row mentions, and statements
beyond `n_lines` are dropped. A file with no statements is still listed so its functions have a
home.

### `coverage.jsonl`

```json
{"test": "tests/test_refund.py::test_refund_partial", "file": "src/ledger/billing/allocate.py",
 "lines": [8, 9, 10, 14]}
{"test": "<import>", "file": "src/ledger/billing/allocate.py", "lines": [1, 3, 5]}
```

One row per (test, file) with the sorted executed lines; `<import>` is the reserved test id for
code that ran outside any test (module import, collection). Lines must be statements of that
file (readers drop the rest, so a module's docstring line or an excluded `def` recorded at import
time does not survive). A file missing from `files.jsonl` keeps its lines and is reported. Two
rows for the same (test, file) are merged. Optional `"arcs": [[from, to], ...]` per row holds the
test's executed branch arcs (validated, not read by coverset today).

### `arcs.jsonl`

```json
{"file": "src/ledger/billing/allocate.py", "arcs": [[8, 9], [8, 12], [9, 10]]}
```

The run-wide executed arcs per file (the union over tests). A branch line whose possible arcs
are not all here is partial.

### `functions.jsonl`

```json
{"key": "89dd83c15378aa33", "path": "src/ledger/billing/allocate.py", "qualname": "allocate_cents",
 "ordinal": 0, "def_line": 8, "body_start": 10, "end_line": 24, "body_hash": "a1b2c3d4",
 "sig_hash": "e5f60718", "params": ["total_cents", "weights"], "kind": "function",
 "types": {"total_cents": {"kind": "int"}, "weights": {"kind": "list", "elem": {"kind": "float"}},
           "return": {"kind": "list", "elem": {"kind": "int"}}}}
```

`key` is `blake2b(path + "\0" + qualname + "\0" + ordinal, digest_size=8)` in hex. It is
optional, because readers compute it themselves (a producer whose platform has no 8-byte BLAKE2b,
such as Node's `crypto`, leaves it out); when present and different, `validate` reports it and the
reader uses its own. `ordinal` numbers same-named regions in one file in source order (overloads,
redefinitions); `(path, qualname, ordinal)` is unique. `def_line` is the first line of the
declaration (decorators included), `body_start` the first body statement after a leading
docstring, `end_line` the last line, with `def_line <= body_start <= end_line`. `body_hash` and
`sig_hash` are the producer's stable hashes of the body and the signature (formatting-insensitive
where the language allows, at most 128 characters); readers compare them only with hashes from
the same producer. `kind` is `function`, `method`, `class` or `module`. `params` lists the
parameter names in order (`*args`-style names may keep their prefix). `types` maps parameter
names (and `return`) to **type descriptors** (below) or `null` when unannotated; it may be
omitted, and names that are not parameters are ignored. The path should be listed in
`files.jsonl`.

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
during which it was seen, at least 1; `example` is a redacted, shortened value repr (at most 200
characters) and `example_test` the test it came from. The repr is the producer's own (`'eu'`,
`<Role.admin: 1>` from Python; `"eu"` or `Currency.EUR` from a TypeScript reporter): readers match
a literal value against both its Python repr and its JSON form, and an enum member `Name.member`
only as a whole token, so `<Role.admin_ro: 2>` shows `Role.admin_ro`, never `Role.admin`. A `sampling` row per traced function gives the total
calls, the tests that were sampled, whether sampling stopped early (`truncated`) and which cap
stopped it (`sample`, `full` or `null`). A non-null `cap` needs `truncated: true`; `truncated: true`
with `cap: null` means the producer does not record which cap stopped it (pytest-coverset does, and
`coverset bundle convert` keeps it).
`obs` rows need `kind`, `func`, `param`, `shape`, `caller`, `samples` (at least 1) and `tests`;
`truncated` defaults to `false`, `example` and `example_test` to `null`. Examples are cut at 200
characters.

### `links.jsonl`

```json
{"test": "tests/test_allocate.py::test_split", "invariant": "cents_conserved"}
```

One row per (test, invariant) pair; the reader adds the invariant to that test's `invariants`
(the union with the ones listed in `tests.jsonl`). A row naming a test missing from `tests.jsonl`
is dropped.

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
validates keys against this vocabulary (each type takes only its own features, each `prefix:`
at most once, `elem:` and `key:` a type token or `mixed`), and a row whose key does not validate is
dropped; `docs/input-classes.md` explains how input classes are derived from them.

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
| `{"kind": "list", "elem": D}`, `{"kind": "set", "elem": D}`, `{"kind": "frozenset", "elem": D}` | collections; `elem` is omitted when the declaration names no element type (bare `list`) |
| `{"kind": "tuple", "elems": [D, ...]}`, `{"kind": "tuple", "elem": D}` | a fixed-size tuple, or a variable-length one (`tuple[int, ...]`); never both fields; neither for a bare tuple |
| `{"kind": "dict", "key": D, "value": D}` | mappings (either field may be omitted) |
| `{"kind": "union", "arms": [D, ...]}` | any of the arms (`Optional[X]` is a union with `none`) |
| `{"kind": "literal", "values": [v, ...]}` | one of the listed JSON scalars |
| `{"kind": "enum", "name": "Role", "members": ["admin", "member"]}` | a closed enumeration (at least one member) |
| `{"kind": "object", "name": "ledger.util.money.Money"}` | a class or interface |
| `{"kind": "any"}` | unknown (an unannotated parameter is `null`, not `any`) |

Descriptors carry no other fields, nest at most 32 levels below the root (the schema unrolls the
recursion into `descriptor`, `descriptor_1` ... `descriptor_32` to say so), and hold at most 256
arms, elements, values or members per node; names are 1 to 512 characters. A `functions.jsonl` row
whose descriptor nests deeper breaks the schema and is dropped whole (`functions.schema`), however
deep it goes: the validator stops descending at 64 levels instead of recursing, so a descriptor
500 levels deep costs one dropped row, not the bundle. A descriptor the schema accepts but that is
still wrong (a `values` field on a `list`) loses only that parameter's type (`functions.descriptor`).

`coverset_model.python_types.descriptor(annotation_text, enums=...)` converts a Python
annotation to a descriptor (`int | None` → union of `int` and `none`; `Literal["a", "b"]` →
literal; `Mapping[K, V]` → dict; an Enum subclass resolved from the sources → enum). The three
abstract sequence names map to `list`, the class an argument usually satisfies: `Sequence[int]`,
`Iterable[int]` and `Collection[int]` are each `{"kind": "list", "elem": {"kind": "int"}}`, so
`Iterable[int] | None` expects the arms `list` and `none`, both of which a test can pass (`set`,
`AbstractSet`, `MutableSet` and `Iterator` stay `set`). A TypeScript reporter does the same from the
TypeScript type (`number` → `float` unless the declared values are integral, `string` → `str`,
`T | undefined` → union with `none`, string-literal unions → literal, `enum` → enum, interfaces
and classes → object). `docs/input-classes.md` lists which classes each descriptor expects.

## Reading a bundle

`coverset_model.io.read_bundle(path)` returns a `NeutralBundle` (the same in-memory contract the
facts store already consumes) for a directory or a `tar.gz`; `validate(path)` returns every
problem (file, row, field, message) without building anything and never raises;
`write_bundle(bundle, dir, manifest=...)` writes one and `pack(dir, archive)` archives it
reproducibly. The Python adapter's own on-disk bundle (`coverage.sqlite`, `coverage.json`,
`shapes.jsonl.gz`, `results.jsonl`, ...) is converted to the same `NeutralBundle` by
`coverset.facts.convert.read_python_bundle`; `coverset bundle convert PATH --to DIR` writes it as
neutral files, and a later `pytest-coverset` writes them directly (`--coverset-neutral`). Given a
neutral bundle, `convert` rewrites it in canonical form and keeps its `language`; either way the
output's `producer` is the converter (or pytest-coverset for a plugin bundle) and
`meta.converted_from` records the source's `format`, `language` and `producer`.
`coverset.lang.read_any_bundle(path)` reads either: a bundle whose `manifest.json` says
`coverset-neutral` is read by the neutral reader whatever its language, anything else by the
Python converter; the orchestrator's ingest uses it.

A bundle is untrusted. The reader **refuses** it (`BundleRejected`, a `BundleError` inside
coverset) when:

| Limit | Default (`coverset_model.io.Limits`, same names as `BundleLimits`) |
|---|---|
| total size of the known files, or of the archive | 200 MiB |
| any gzip stream (the archive, each `.jsonl.gz`) | 10 × its compressed size, at least 16 MiB, at most 500 MiB |
| archive members | 256, flat names, regular files only |
| `manifest.json` | 1 MiB; `format`, `version` and the schema must hold |
| lines per file (`max_rows`, `max_coverage_rows`; blank, oversized and malformed lines count) | 2,000,000 in `coverage.jsonl`, 200,000 in every other file |
| wall-clock time of one read (`max_seconds`) | 120 s |
| distinct test ids, paths, shape keys | 50,000, 50,000, 20,000 |
| function regions | 200,000 |
| (test, file, line) coverage cells | 4,000,000 |
| a required file is missing, a file is a symlink or device, both `.jsonl` and `.jsonl.gz` exist | refused |

The row caps are ten times what the plan's scale needs: `scripts/bench_facts.py`'s run (10,000
tests × 2,000 lines over 2,000 files, each test touching about 20 files) is 200,000 coverage rows
for the whole run and 20,000 function regions, and its 1,000,000 `call_shapes` rows over 30 runs
are about 33,000 shape rows per run; the demo's whole run is 139 coverage rows, 62 functions and
332 shape rows. The cell cap keeps one shard's ingest near 300 MB of memory (read plus the facts
tables; `docs/security.md` B14 has the measurement); the plan's 20 million cells per run fit in
five shards or more (`bench_facts.py` uses eight, 2.5 million each). The file is read as a stream
(only the current line is in memory), every line costs at most one schema violation (the check
stops at the first), and the coverage lines kept are sorted tuples sharing one int object per line
number, about 8 bytes per cell.

Everything else is per row: a row that breaks its schema, names an unsafe path, reuses a test id
or the reserved `<import>` id, has a line longer than 1 MiB, invalid UTF-8, a lone surrogate or a
non-finite number, a shape key outside the grammar, a function whose lines do not nest or whose
`(path, qualname, ordinal)` repeats, is **dropped**; executed lines that are not statements are
dropped from their row; messages, `fail_sig` and examples are **cut**. Each is counted in
`meta["convert"]["dropped"]` as `<file>.<code>` (`tests.path`, `coverage.not_statement`, ...).
With a head tree (`allowed_paths`), rows naming any other path are dropped too
(`<file>.paths_not_in_head`).

The reader fills two `meta` keys itself: `meta["bundle"]` (the manifest's `format`, `version`,
`language`, `producer`, `created_at`, `shard`) and `meta["convert"]` (`bundle_sha256` over the
files read, the file names, rows per file, the drop counts, `bundle_bytes`). `write_bundle` writes
`conform(bundle)`, what the format keeps of an in-memory bundle (tests sorted by id, every test
with a result also collected, non-statement lines dropped, long texts cut), so
`read_bundle(write_bundle(b)) == conform(b)` apart from those two keys.

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
