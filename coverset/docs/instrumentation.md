# Instrumentation: pytest-coverset

`pytest-coverset` is the half of coverset that runs inside the worker, next to the code under test. It is a pytest plugin (`packages/pytest-coverset/src/pytest_coverset/`) with one runtime dependency besides pytest: `coverage>=7.16,<8`. It records four things:

- per-test line and branch coverage, one coverage.py context per test;
- the shapes of the arguments and return values of in-repo functions, through `sys.monitoring`;
- each test's final outcome, streamed as JSON lines on stdout;
- a facts bundle, `bundle.tar.gz`, that the control plane pulls once the shard ends.

It also orders tests by a scores file and registers the `invariant(id)` marker. The plugin's own README (`packages/pytest-coverset/README.md`) is the reference card for its options; this page explains how each part works and why.

## Requirements

| Requirement | Why | Where enforced |
|---|---|---|
| Python 3.12 or later | the shape tracer uses `sys.monitoring` (PEP 669) | `requires-python = ">=3.12"` in `packages/pytest-coverset/pyproject.toml`; `lang/python.py` refuses `[tool.coverset] python` below 3.12; without `sys.monitoring` the tracer disables itself and records `shapes_disabled_reason` |
| pytest 8.2 or later | `@file` argument files, used to hand each shard its node ids | `pytest>=8.2` |
| coverage.py 7.16 | the ctrace core with dynamic contexts, SQLite data schema 7 | `coverage>=7.16,<8`; the workspace pins 7.16.2 |
| no other `sys.settrace` tracer | coverage's ctrace core needs the trace function | a foreign tracer is a `pytest.UsageError` (exit 4); test `test_foreign_tracer_is_a_usage_error` |

The control plane itself runs on Python 3.13. Instrumented repositories need 3.12 or 3.13 (3.14 is untested).

## Turning it on

Installing the plugin changes nothing about a normal run except registering the marker. Instrumentation starts with `--coverset-out DIR`. Test `test_inert_without_out` checks that a run without it writes no coverage data and no events.

| Option | Default | Effect |
|---|---|---|
| `--coverset-out DIR` | off | write the bundle to `DIR`; turns instrumentation on |
| `--coverset-scores FILE` | off | JSON `{nodeid: score}`; run the riskiest tests first |
| `--coverset-source PKG` | all in-repo code | package name or path to measure and trace (repeatable) |
| `--coverset-trace-module MOD` | none | extra module, and its submodules, to trace (repeatable) |
| `--coverset-full-trace FILE` | none | repo-relative paths, one per line, traced without a sample cap |
| `--coverset-shapes on\|off` | `on` | record argument and return shapes |
| `--coverset-repr-examples off\|scalars\|all` | `scalars` | which redacted example values to keep |
| `--coverset-stop-file PATH` | none | once `PATH` exists, skip the remaining tests, write the bundle, exit 2 |
| `--coverset-shape-samples N` | 2048 | sampled calls per function before sampling stops |
| `--coverset-shape-settle N` | 64 | consecutive calls with no new shape before sampling stops |

The orchestrator builds the command line through `PythonAdapter.test_command` (`src/coverset/lang/python.py`). It always adds `--continue-on-collection-errors -p no:cacheprovider -q`, plus `-n 0` when `pytest-xdist` is in the environment and `--no-cov` when `pytest-cov` is.

## Per-test coverage

### Why the ctrace core

coverage.py has two tracer cores that matter here: the C `settrace` tracer (`ctrace`) and the `sys.monitoring` core (`sysmon`). Only ctrace supports dynamic contexts. coverage.py's own core selection says so: with dynamic contexts requested, `coverage/core.py` refuses sysmon with the reason "it doesn't yet support dynamic contexts". With branch coverage on, the same function also refuses sysmon before Python 3.14 ("sys.monitoring can't measure branches in this version"). On 3.14 sysmon becomes the default, so the core must be chosen explicitly.

`CoverageContext` (`coverage_ctx.py`) therefore builds `coverage.Coverage(data_file=<out>/coverage.sqlite, data_suffix=False, branch=True, source=<sources or None>, config_file=True)` and, before `start()`, sets:

| Option | Value | Why |
|---|---|---|
| `run:core` | `ctrace` | dynamic contexts (the constructor has no `core=` argument) |
| `run:relative_files` | `True` | paths relative to the working directory, which is the repo root in a worker |
| `run:dynamic_context` | `None` | the plugin switches contexts itself |
| `run:context` | `None` | no static context prefix, so a context is exactly a node id |
| `run:parallel` | `False` | one data file per shard |
| `run:disable_warnings` | five slugs | `module-not-imported` and friends are expected when a shard imports part of the sources |

`meta.json` records the tracer coverage.py actually used (`"tracer": "CTracer"` in `test_bundle_contents_and_schemas`) and `"core": "ctrace"`.

coverage.py warnings are also filtered around every call, so a target repository that runs with `-W error` or `filterwarnings = error` keeps working (`test_strict_warnings_do_not_break_the_run`).

### One context switch per test

Coverage starts in `pytest_load_initial_conftests`, before conftest files are imported, so their import-time lines are measured. A `pytest_runtest_protocol` hook wrapper calls `switch_context(item.nodeid)` once per test. Setup, call and teardown all land in the test's context. That is a deliberate simplification: a fixture's lines count for the test that requested it.

Code that runs before the first test (imports, collection) stays in coverage.py's `''` context. The bundle reader maps `''` to the pseudo test id `<import>` (`facts/convert.py`). Test `test_one_context_per_test` checks that the recorded contexts are exactly `''` plus one per node id, including ids that contain `|`.

### When data reaches disk

coverage.py writes the current context's data to SQLite on every `switch_context`, so a test's lines are on disk as soon as the next test starts. After a `failed` or `error` result the plugin flushes immediately (`cov.get_data()`). At session end it stops, saves, and writes `coverage.json` with `cov.json_report()` (format 3). There is no separate timer: the plan's "save every 50 tests or 30 s" is covered by the per-switch write.

### Stopping early

Two graceful stops end with exit code 2, a complete bundle, and the `bundle_written` and `done` events. Tests that did not finish count as `not_run`.

| Stop | How it works | Test |
|---|---|---|
| STOP file | The plugin checks `--coverset-stop-file` before each test. When it exists, the plugin sets `session.shouldstop`, skips the remaining items and finalises. A running test finishes first. | `test_stop_file_ends_the_run_and_keeps_the_bundle` |
| SIGTERM | The first SIGTERM raises `KeyboardInterrupt`, which interrupts even a hung test. While a result is being recorded the interrupt is deferred until the record is complete. Later SIGTERMs, and any during finalisation, are ignored. | `test_sigterm_interrupts_a_hung_test_and_writes_the_bundle` |

SIGKILL loses the in-memory shapes and the bundle. The LocalExecutor sends SIGTERM to the process group and SIGKILL 5 s later (`executors/local.py`, `KILL_GRACE_S`). Under `fail_fast = "cancel_remaining"` the runner writes the STOP file and waits up to 20 s for bundles before cancelling ([docs/orchestrator.md](orchestrator.md#retries-flaky-tests-and-fail-fast)).

### Exit codes

The plugin does not invent exit codes; these are pytest's, and the plugin reaches 2 through `shouldstop` or the interrupt.

| Code | Meaning in a coverset shard |
|---|---|
| 0 | every test passed |
| 1 | some test failed (also after collection errors with `--continue-on-collection-errors`, `test_collection_errors_still_write_a_bundle`) |
| 2 | interrupted: STOP file or SIGTERM |
| 4 | usage error: a foreign tracer, or an unreadable `--coverset-scores` file |
| 5 | no tests collected |

The runner treats 0, 1 and 5 as normal shard ends, and 2 as normal only after it asked for a stop (`runner._shard_error`).

## The shape tracer

`ShapeTracer` (`shapes.py`) records which kinds of value flowed into and out of each traced function. It is a second `sys.monitoring` tool that coexists with coverage's ctrace core.

### Tool ids

CPython gives `sys.monitoring` tools ids 0 to 5 (asking for 7 raises "invalid tool 7 (must be between 0 and 5)"); `sys.settrace`, which ctrace uses, is implemented on reserved ids outside that range. coverset tries ids in the order 4, 5, 2 and records the one it got as `meta.json` `tool_id`. It avoids 1 (`sys.monitoring.COVERAGE_ID`, which coverage's sysmon core claims) and 3 (Hypothesis's scrutineer: `hypothesis/internal/scrutineer.py` sets `MONITORING_TOOL_ID = 3`). If none of 4, 5 and 2 is free, shapes are off for the session and `shapes_disabled_reason` names the owners (`test_tool_id_fallback`).

### Discovery: a global `PY_START`, then local events

1. The tracer enables one global event: `PY_START`.
2. The first `PY_START` of each code object decides, once, whether it is a target. The decision is cached by code object.
3. A non-target returns `sys.monitoring.DISABLE`, so CPython stops reporting that code location to this tool.
4. A target gets local events with `set_local_events(tool_id, code, PY_START | PY_RETURN)`. Generators and async generators get `PY_START` only.

A code object is a target when all of these hold:

- its file is inside the repository (`norm_path` against the git root);
- the file is not a test module or conftest (`test_*.py`, `*_test.py`, `conftest.py`);
- it is a function body (`CO_OPTIMIZED`), not module or class body code, and not a `<genexpr>`-style code object (`<lambda>` is allowed);
- its file is in the full-trace list, or it matches `--coverset-source` (a package name matched against the module's `__name__`, or a path) or `--coverset-trace-module`. With none of those given, every in-repo file outside virtualenv directories matches.

`restart_events()` is never called: it is global across tools and would undo other tools' `DISABLE` decisions.

### Adaptive sampling and the full-trace list

Sampling is adaptive per code object per session. A function stops being sampled once 64 consecutive calls (`--coverset-shape-settle`) produced no new `(parameter, shape)` pair, or once 2,048 calls (`--coverset-shape-samples`) were sampled. The tracer then switches the code object's local events off, returns `DISABLE`, and marks the function truncated. Every observation row of that function carries `trunc: true`, and its `func_sampling` row says `truncated: true`.

Functions in files on the full-trace list are never capped. The orchestrator writes that list into each worker as `run/full-trace.txt`: the PR's changed source files plus the file of every invariant target (`orchestrator/planner.py`). Test `test_adaptive_cap_and_full_trace` exercises both: a hot function stops after more than 64 and fewer than 80 calls, while its full-trace twin records all 300.

Why truncation matters downstream: a class missing from a truncated function is reported as "not observed in N sampled calls", never as "never exercised" ([docs/input-classes.md](input-classes.md#never-exercised-or-not-observed)).

### What a sample reads

On `PY_START` the callback gets `(code, offset)`. It fetches the callee frame with `sys._getframe(1)` and reads only the parameters from `frame.f_locals`: `co_varnames[:co_argcount + co_kwonlyargcount]`, then `*args` and `**kwargs` when `co_flags` says they exist. Varargs keep their real names with the stars (`*args`, `**kw`). For a method's `self` or `cls` only the receiver's class is recorded, under the pseudo-parameter `<receiver>`.

On `PY_RETURN` the callback gets the return value directly (`retval`) and records its shape under the pseudo-parameter `return`.

Each observation also records its caller. When the calling frame belongs to an in-repo, non-test function, the caller is that function's `(path, co_qualname, co_firstlineno)`. Otherwise the caller is the running test's node id, or `<import>` outside any test. The flow ribbon is drawn from these callers.

Raised exceptions are not recorded: `PY_UNWIND` can only be enabled globally, not per code object.

### Safety rules

The tracer runs inside the code under test, on every traced call. It must never change behaviour.

| Rule | How |
|---|---|
| No re-entrancy | a thread-local `busy` flag; a callback that fires inside a callback returns at once |
| Never raise into user code | the callback body is wrapped in `try/except BaseException`; errors are counted in `meta.json` `tracer_errors`. `KeyboardInterrupt` alone propagates, so a SIGTERM stop is not swallowed. |
| One bad value does not drop the call | each parameter is observed in its own `try` |
| No user code runs | features are computed only for values whose type is exactly `NoneType`, `bool`, `int`, `float`, `str`, `bytes`, `list`, `tuple`, `dict`, `set` or `frozenset`. User `__len__`, `__iter__`, `__bool__`, `__eq__`, `__hash__` and `__repr__` are never called (`test_user_dunders_are_never_called`). Class names are read through `type`'s own descriptors, so a metaclass cannot interfere. |
| Bounded work | containers are summarised over at most 32 leading elements, read by index (`list`, `tuple`) or builtin iteration (`set`, `dict`); string kinds are skipped above 2,048 characters; whitespace and digit features scan at most 4,096 characters |
| No objects kept | the aggregate holds shape keys, counts, test indexes and redacted example strings, never the values |
| Clean teardown | `pytest_unconfigure` switches every event off, unregisters both callbacks and frees the tool id; a second session in the same process works (`test_second_session_in_same_process`) |

Test `test_coverage_identical_with_and_without_shapes` checks that the tracer does not change coverage: the `(context, file, lines, arcs)` rows are identical with shapes on and off.

### Shape keys

A shape key is a type token followed by `|`-joined feature tokens sorted alphabetically, so equal values give equal keys (`features.py`, `shape_key`). Examples recorded by the plugin's tests:

```
int|pos
float|nan
float|inf|inf-neg
str|ascii|kind:email/multi-label-domain|len:11-100
list|any_neg|elem:int|len:2-10|order:unsorted
list|elem:tuple|len:1|pair:gt
obj:fixturepkg.accounts.Account
```

| Type | Feature tokens |
|---|---|
| `NoneType` | none |
| `bool` | `true` or `false` |
| `int` | `neg`, `zero` or `pos`; `big` above 2**31 in absolute value |
| `float` | `nan`; `inf` (plus `inf-neg` for minus infinity); else `neg`, `zero` or `pos`, plus `integral` |
| `str` | `len:<0\|1\|2-10\|11-100\|101-1000\|>1000>`; `empty`, `whitespace`, or `ascii`/`unicode`; `ws-edge`; `digits`; `kind:<email/sub\|numeric\|quantity/sub\|iso-date\|url>` |
| `bytes` | `len:<bucket>` |
| `list`, `tuple`, `set`, `frozenset`, `dict` | `len:<bucket>`; `elem:<type\|mixed>`; for dicts `key:<type\|mixed>`; for all-number elements `any_zero`, `all_zero`, `any_neg`, `any_nan`, `any_inf`, `sum_zero`; for sequences of at least two comparable elements `order:<sorted\|reverse\|unsorted\|n/a>` and `dups`; for sequences of numeric pairs `pair:<lt\|eq\|gt\|mixed>` |
| anything else | `obj:<module>.<qualname>`, plus `dataclass` or `pydantic` when the class is one |

Two details differ from the plan's Addendum B. Infinities never count as the finite sign classes: `+inf` is `float|inf` and `-inf` is `float|inf|inf-neg`. The numeric string kind is spelled `kind:numeric` in keys; the input class it maps to is `numeric-string`. How keys become input classes is in [docs/input-classes.md](input-classes.md).

### Example values and redaction

Each `(function, parameter, shape, caller)` aggregate keeps one example value, made by `example_repr` (`redact.py`):

| `--coverset-repr-examples` | Examples kept |
|---|---|
| `off` | none |
| `scalars` (default) | `None`, `bool`, `int`, `float`, `str`, `bytes` |
| `all` | the scalars plus builtin containers |

Redaction, in order:

1. The value is replaced by `<redacted:len=N>` (`<redacted>` without a length) when the name it is bound to matches `(?i)pass|secret|token|key|auth|cookie|session`. For a parameter that name is the parameter's; for `return` it is the function's name (`make_reset_token` returns a token); inside a container example it is a `str` dict key.
2. A `reprlib.Repr` with `maxstring=48, maxother=48, maxlevel=2` formats exact builtin types only; anything else prints as `<module.Class>` without calling its `__repr__`. Ints wider than 128 bits print as `<int N bits>`.
3. The text is scrubbed with the same credential patterns as the control plane's `coverset.logging` (PEM blocks, `x-access-token:...@`, `gh[opsur]_...`, `github_pat_...`, `sk-ant-...`, `Authorization` headers).
4. Every email keeps its domain but loses its local part: `bob@eng.acme.com` becomes `'b***@eng.acme.com'`.
5. The result is cut to 200 characters.

Tests: `test_example_redaction`, `test_return_values_of_secret_named_functions_are_redacted`, `test_secret_dict_keys_are_redacted_in_container_examples`, `test_huge_ints_and_long_bytes_are_summarised`, `test_hostile_values_keep_every_parameter_and_hide_secrets`.

### What is never recorded

- the values themselves: only shape keys, counts, and one redacted example of at most 200 characters per aggregate;
- container contents as examples unless `--coverset-repr-examples all`;
- anything about a non-builtin object beyond its class name and the `dataclass`/`pydantic` flag;
- local variables other than parameters, and the return values of generators;
- exceptions;
- anything at all from test modules and conftest files, or with `--coverset-shapes off`.

## Events on stdout

The runner learns about progress from stdout. Each event is one line:

```
\x1ecoverset <nonce> {"kind":"test","t":1791725241.26,...}\n
```

| Part | Rule |
|---|---|
| prefix | the ASCII record separator `\x1e`, the word `coverset`, a space, the nonce, a space |
| nonce | a fresh 128-bit hex value per exec in `COVERSET_EVENT_NONCE`; `-` when unset |
| body | compact JSON, ASCII only (`ensure_ascii=True`), so any stdout encoding carries it |
| stream | `sys.__stdout__`, flushed per event; a newline is written first if pytest's terminal writer is mid-line |

The plugin removes `COVERSET_EVENT_NONCE` from `os.environ` when the session starts, before conftest files load, so tests and their subprocesses do not inherit it (`test_nonce_is_hidden_from_tests_and_their_subprocesses`). The nonce separates events from log text that merely looks like one. It is not a security boundary: test code shares the process and can still find it. That is why `results.jsonl` in the bundle is the authoritative record and the runner reconciles streamed rows against it.

Readers must split stdout on `\n` only. Python's `str.splitlines()` treats `\x1e` as a line break.

| Kind | Payload (besides `kind` and `t`, the UNIX time) |
|---|---|
| `collected` | `count`, `errors` |
| `test` | `nodeid`, `file`, `outcome`, `duration_s`, `failed_phase`, `fail_sig`, `longrepr`, `truncated`, `reason`, `start`, `stop` |
| `heartbeat` | `done`, `total`, `elapsed_s`; emitted before a test when no event was written for 10 s |
| `bundle_written` | `path`, `sha256`, `bytes` |
| `done` | `exit_code`, `counts` (every outcome plus `not_run`) |

`outcome` is one of `passed`, `failed` (the body failed, including a strict XPASS), `error` (setup or teardown failed), `skipped`, `xfailed`, `xpassed`. `fail_sig` is the blake2b-8 hex digest of the exception type plus the crash location, so the same failure on two commits gets the same signature even when its message differs (`test_fail_sig_is_stable_across_messages`). `longrepr` is cut to 8 KiB of UTF-8 with `truncated` set.

## The bundle

At session end the plugin writes every file below into `--coverset-out`, then packs them into `bundle.tar.gz`. The tarball is flat, streamed to disk, renamed into place atomically, and has owner ids and names cleared. Its sha256 and size go out in the `bundle_written` event. An error while writing the coverage, shapes or static files is recorded in `meta.json` `errors`, and the remaining files are still written.

Paths in `shapes.jsonl.gz`, `results.jsonl` and `collect.json` are git-root-relative POSIX (the rootdir when there is no `.git`). Paths in `coverage.sqlite` and `coverage.json` are relative to the working directory, which is the repo root in a worker. `paths.py` holds the one normaliser, so coverage file keys, tracer function keys, `git diff` paths and AST indexes join without translation (`test_paths_agree_between_coverage_and_shapes`).

| File | Schema |
|---|---|
| `coverage.sqlite` | coverage.py's SQLite data file, schema 7: tables `file`, `context`, `line_bits` (numbits blobs), `arc` (with branch coverage on, lines are the positive arc endpoints). One context per node id plus `''`. |
| `coverage.json` | `cov.json_report()` format 3. coverset reads `meta.format`, and per file `executed_lines`, `missing_lines`, `excluded_lines`, `executed_branches`, `missing_branches`. The report also carries `functions` and `classes` regions (`test_coverage_json_fields`). |
| `shapes.jsonl.gz` | gzip JSON lines: one header, then rows (below). Header-only when shapes are off. |
| `results.jsonl` | one row per finished test with exactly the `test` event fields; written as tests finish and authoritative over streamed events |
| `collect.json` | `{"items": [{nodeid, file, line, markers, invariant_ids, hypothesis}], "errors": [{nodeid, longrepr, truncated}]}`. `line` is 1-based (the first decorator line). `hypothesis` is true for `@given` tests. |
| `durations.json` | `{nodeid: duration_s}` |
| `invariant_links.jsonl` | `{"test_id": <nodeid>, "invariant_id": <id>}` per marker link |
| `meta.json` | `schema`, `plugin_version`, `python`, `coverage_version`, `pytest_version`, `core`, `tracer`, `context_format`, `tool_id`, `git_root`, `repo_root`, `rootdir`, `cwd`, `sources`, `trace_modules`, `full_trace`, `shapes`, `shapes_disabled_reason`, `repr_examples`, `shape_samples`, `shape_settle`, `functions_traced`, `functions_truncated`, `tracer_errors`, `hypothesis` (`version`, `deadline_ms`), `exit_code`, `counts`, `stop_requested`, `errors`, `started_at`, `finished_at`, `timings` (`session_wall_s`, `tracer_callback_s`, `tracer_events`) |

### `shapes.jsonl.gz`

Rows refer to the header's lists by index, so long strings appear once.

```json
{"kind": "header", "version": 1,
 "funcs":   [["src/ledger/billing/allocate.py", "allocate_cents", 4], ...],
 "shapes":  ["int|pos", ...],
 "tests":   ["tests/test_allocate.py::test_allocate_cents_examples[100-weights0-expected0]", ...],
 "callers": [{"kind": "test", "nodeid": "<import>"},
             {"kind": "test", "nodeid": "tests/test_allocate.py::test_allocate_cents_examples[100-weights0-expected0]"}, ...]}
{"kind": "obs", "f": 0, "p": "total_cents", "s": 0, "c": 1, "n": 1, "t": [0], "trunc": false, "ex": "100", "ext": 0}
{"kind": "func_sampling", "f": 0, "calls": 213, "tests": [0, 1, 2, ...], "truncated": false}
```

| Field | Meaning |
|---|---|
| `funcs[i]` | `[path, co_qualname, co_firstlineno]` (`co_firstlineno` is the first decorator line, else the `def` line) |
| `obs.f`, `obs.s`, `obs.c` | indexes into `funcs`, `shapes`, `callers` |
| `obs.p` | the parameter name, `*args`/`**kwargs` style for varargs, `return`, or `<receiver>` |
| `obs.n` | samples aggregated in this row |
| `obs.t` | indexes into `tests` of every test during which the row was observed |
| `obs.trunc` | the function's sampling was capped |
| `obs.ex`, `obs.ext` | the redacted example (or null) and the index of the test that produced it |
| `func_sampling` | one row per traced function: sampled `calls`, sampled `tests`, `truncated` |

The block above is an excerpt of the bundle from the hand run described in [Running it by hand](#running-it-by-hand), with the lists shortened (`...`). A caller of kind `func` has the form `{"kind": "func", "path": ..., "qualname": ..., "line": ...}`.

## Hypothesis

Hypothesis and coverset coexist on 3.12 and later. Hypothesis's scrutineer claims `sys.monitoring` tool id 3; coverset never asks for 3. A `@given(st.floats())` test is traced like any other, and the plugin's tests check that `nan` and an infinity are recorded for such a parameter (`test_recorded_shapes`).

Tracing makes every traced call slower, and Hypothesis's default deadline of 200 ms per example can then fail tests that pass without coverset. Register a profile without a deadline and load it in `conftest.py`, as the demo app does (`examples/demo-app/tests/conftest.py`):

```python
from hypothesis import settings

settings.register_profile("coverset", deadline=None, print_blob=True)
settings.load_profile("coverset")
```

The plugin does not change Hypothesis settings itself. It records the active default deadline in `meta.json` (`hypothesis.deadline_ms`; `null` means no deadline), so a reader can tell which profile a shard ran under.

## Overhead

Per-test coverage with the ctrace core plus the shape tracer costs time. `packages/pytest-coverset/scripts/measure_overhead.py` measures it: it runs a suite in three modes (plain pytest, coverage only, coverage plus shapes), interleaved, in a temporary copy, and prints the median wall time of each plus `overhead_x` (coverage plus shapes ÷ plain).

```sh
uv run python packages/pytest-coverset/scripts/measure_overhead.py
uv run python packages/pytest-coverset/scripts/measure_overhead.py --target examples/demo-app --source ledger
```

The measured multiplier is in [docs/numbers.json](numbers.json) and [docs/benchmarks.md](benchmarks.md). This page quotes no figure.

## Running it by hand

From the root of a repository whose tests import the code under `src/` (the demo app sets `pythonpath = ["src"]` in its pytest configuration):

```sh
python -m pytest --coverset-out out --coverset-source ledger
```

`out/` then holds the eight files and `bundle.tar.gz`. On a copy of `examples/demo-app` at the branch head this ran 97 tests, traced 34 functions, capped 4 of them, and recorded `tool_id` 4 and `tracer` `CTracer` in `meta.json`.

The event lines go to stdout among pytest's own output. Set `COVERSET_EVENT_NONCE` to pick the nonce; without it the prefix is `\x1ecoverset - `.

What a shard runs, in full, is closer to this:

```sh
COVERSET_EVENT_NONCE=<32 hex> PYTHONPATH=<repo>/src:<repo> \
python -m pytest @run/shard.args \
  --coverset-out run/out --coverset-scores run/shard.scores.json \
  --coverset-source ledger --coverset-full-trace run/full-trace.txt \
  --coverset-shapes on --coverset-stop-file run/STOP \
  --continue-on-collection-errors -p no:cacheprovider -q --rootdir=<repo>
```

`run/shard.args` holds one node id per line, verbatim, with no blank lines (a blank line would collect the whole rootdir). Duplicates collapse. `run/shard.scores.json` holds rank-derived scores (`len(shard) - index`) so the plugin's sort reproduces the planner's order (`analysis/sharding.py`, `shard_scores`).

The plugin's sort key is `(-max_score(module), module_path, -max_score(class), class_name, -score, nodeid)`. The riskiest module runs first and, within it, the riskiest class, so module- and class-scoped fixtures still set up once per group. Unknown node ids score 0. `[tool.coverset] reorder = false` makes the runner pass no scores file, and the shard then runs in the args-file order.

To test the plugin itself:

```sh
uv run pytest packages/pytest-coverset/tests -q -p no:cacheprovider
```

The plan's check on Python 3.12, outside the workspace lock, is `uv run --isolated --no-project --python 3.12 --with ./packages/pytest-coverset --with pytest==9.1.1 --with hypothesis==6.168.5 pytest packages/pytest-coverset/tests`. It was not re-run for this page.
