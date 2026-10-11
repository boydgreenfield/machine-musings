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

The reader treats the bundle as hostile: sizes, gzip ratios, line lengths, row counts, path
rules and id lengths are capped (`Limits`), rows that break the schema are dropped and counted,
lines that are not statements of their file are dropped. `coverset bundle validate PATH` prints the
same problems from the command line.

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
`read_bundle(write_bundle(b)) == conform(b)` apart from the reader's own `meta` keys.

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
