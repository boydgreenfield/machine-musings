# coverset-model

coverset's semantic-coverage model, with no dependencies (Python 3.12+): the **neutral bundle**
a test run leaves behind, the **type descriptors** that say what a parameter accepts, the
**shape-key** grammar that says what it was given, and the **input-class catalogue** that turns
the two into "which kinds of argument did the tests never pass in".

The on-disk format is specified in [`docs/neutral-bundle.md`](../../docs/neutral-bundle.md);
the JSON Schemas are in [`docs/schema/`](../../docs/schema/). `pytest-coverset` produces it for
Python, a Jest or Vitest reporter for TypeScript; coverset ingests it whatever the language.

| Module | What it holds |
|---|---|
| `coverset_model.neutral` | `NeutralBundle` and its rows (`CollectedItem`, `TestRow`, `StaticFile`, `ShapeObs`, `FuncSampling`, `FunctionRegion`, ...), `func_key` |
| `coverset_model.io` | `read_bundle`, `write_bundle`, `validate`, `inspect`, `conform`, `pack`, `Limits`, `Problem` |
| `coverset_model.schema` | the JSON Schemas as dicts, `check` (a validator for the subset they use), `write_all` |
| `coverset_model.types` | `TypeDescriptor`, `to_json`/`from_json`, `expected_families` |
| `coverset_model.python_types` | `descriptor("int \| None", enums=...)`: Python annotation text → descriptor |
| `coverset_model.shape_keys` | `parse`, `validate`, `canonical`, the token tables |
| `coverset_model.catalogue` | `Catalogue`, `FunctionInputCov`, `function_input_cov`, `render_markdown`, `suggest_annotation`, `cap_hits` |

## Reading and checking a bundle

```python
from coverset_model import read_bundle, validate

for problem in validate("out/neutral"):  # a directory or a tar.gz; never raises
    print(problem)  # tests.jsonl:3: id: test '...' is listed twice
bundle = read_bundle("out/neutral")  # BundleRejected on a hard limit
print(len(bundle.tests), bundle.meta["convert"]["dropped"])
```

The reader treats the bundle as hostile. `Limits` caps, and refuses the bundle beyond:

| Field | Default | What |
|---|---|---|
| `max_bundle_bytes` | 200 MiB | the known files of a directory, or the archive |
| `gzip_ratio`, `gzip_floor_bytes`, `gzip_max_bytes` | 10, 16 MiB, 500 MiB | each gzip stream (archive, `.jsonl.gz`) expands to at most 10 × its size, never below 16 MiB nor above 500 MiB |
| `max_members` | 256 | archive members (flat, regular files only) |
| `max_manifest_bytes` | 1 MiB | `manifest.json` |
| `max_coverage_rows`, `max_rows` | 2,000,000, 200,000 | lines of `coverage.jsonl`, lines of every other file (blank and malformed lines count) |
| `max_seconds` | 120 s | wall-clock time of one `read_bundle` or `validate` |
| `max_tests`, `max_paths`, `max_shape_keys`, `max_functions` | 50,000, 50,000, 20,000, 200,000 | distinct test ids, paths, shape keys, function regions |
| `max_coverage_cells` | 4,000,000 | distinct (test, file, line) cells, about 8 bytes each once read |

and drops (or clips), counting each in `meta["convert"]["dropped"]`: a line over
`max_json_line_bytes` (1 MiB), a row that breaks its schema (the check stops at the first
violation and never descends more than 64 levels, so a descriptor 500 levels deep costs one row),
unsafe paths, ids over `max_nodeid_chars` (512), lines that are not statements of their file,
examples over `max_repr_chars` (200) and messages over `max_longrepr_chars` (8 KiB). The numbers
and what they were measured against are in `docs/neutral-bundle.md` ("Reading a bundle") and
`docs/security.md` (B14). `coverset bundle validate PATH` prints the same problems from the command
line.

## Writing one

```python
from coverset_model import write_bundle

write_bundle(
    bundle,
    "out/neutral",
    manifest={
        "language": "typescript",
        "producer": {"name": "vitest-coverset", "version": "0.1.0"},
        "created_at": "2026-10-11T19:04:05Z",
        "shard": 0,
    },
)
```

`write_bundle` writes `conform(bundle)`: what the format keeps of an in-memory bundle, so
`read_bundle(write_bundle(b)) == conform(b)` apart from the reader's own `meta` keys. Every bundle
file an earlier write left in the directory (the other variant, `links.jsonl`, an optional file
with no rows now) is removed, and each file is created afresh, never written through a symlink.

## Input classes from declared types

```python
from coverset_model import Catalogue, TypeDescriptor as T, function_input_cov

types = {"weights": T.list_of(T.scalar("float")), "total_cents": T.scalar("int")}
cov = function_input_cov(region, observations, sampling, annotations=types)
print([c.name for c in cov.missing])  # ['total_cents:negative', 'weights:zero-element', ...]
```

`docs/input-classes.md` lists the classes each descriptor expects. Python annotation text is
accepted wherever a descriptor is and converted with `python_types.descriptor`.

## Tests

```sh
uv run pytest packages/coverset-model/tests
```
