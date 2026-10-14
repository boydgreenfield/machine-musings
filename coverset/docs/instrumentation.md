# Instrumentation: pytest-coverset

`pytest-coverset` is the half of coverset that runs inside the worker, next to the code under test. It is a pytest plugin (`packages/pytest-coverset/src/pytest_coverset/`) with one runtime dependency besides pytest: `coverage>=7.16,<8`. Its `pyproject.toml` requires exactly `coverage` and `pytest` and nothing else. It records four things:

- per-test line and branch coverage, one coverage.py context per test;
- the shapes of the arguments and return values of in-repo functions, through `sys.monitoring`;
- each test's final outcome, streamed as JSON lines on stdout;
- a facts bundle, `bundle.tar.gz`, that the control plane pulls once the shard ends, and on request the same facts as a language-neutral bundle, `neutral.tar.gz` ([The neutral bundle](#the-neutral-bundle)).

It also orders tests by a scores file and registers the `invariant(id)` marker. The plugin's own README (`packages/pytest-coverset/README.md`) is the reference card for its options; this page explains how each part works and why.

## Requirements

| Requirement | Why | Where enforced |
|---|---|---|
| Python 3.12 or later | the shape tracer uses `sys.monitoring` (PEP 669) | `requires-python = ">=3.12"` in `packages/pytest-coverset/pyproject.toml`; `lang/python.py` refuses `[tool.coverset] python` below 3.12; a plugin forced onto 3.11 stops pytest with a usage error naming the 3.12 floor (exit 4) instead of a raw `TypeError` (`test_python_below_3_12_is_a_clear_usage_error`) |
| pytest 8.2 or later | `@file` argument files, used to hand each shard its node ids | `pytest>=8.2` |
| coverage.py 7.16 | the ctrace core with dynamic contexts, SQLite data schema 7 | `coverage>=7.16,<8`; the workspace pins 7.16.2 |
| no other `sys.settrace` tracer | coverage's ctrace core needs the trace function | a foreign tracer at start is a `pytest.UsageError` (exit 4), test `test_foreign_tracer_is_a_usage_error`; one installed later is detected ([When the tracer is lost](#when-the-tracer-is-lost)) |
| pytest-xdist: local workers only (optional) | with `-n N` every worker runs the plugin into its own directory and the controller merges one bundle ([Running in parallel](#running-in-parallel)); the merge reads every worker's files from this disk and working directory | `--tx ssh=...`, `socket=...`, `popen//chdir=...` and `--rsyncdir` are a `pytest.UsageError` (`test_remote_workers_are_a_usage_error`) |

The control plane itself runs on Python 3.13. Instrumented repositories need 3.12 or 3.13 (3.14 is untested).

## Turning it on

Installing the plugin changes nothing about a normal run except registering the marker. Instrumentation starts with `--coverset-out DIR` (what the runner passes) or with `--coverset` (what a developer types). Test `test_inert_without_out` checks that a run without either writes no coverage data and no events.

On a laptop, three lines give the pages for the local suite:

```sh
pytest --coverset                  # instrument the run; the bundle goes to <rootdir>/.coverset
coverset report .coverset --open   # build the pages and open them
```

`--coverset` (or `COVERSET=1` in the environment) turns instrumentation on with local defaults, and every explicit option still wins:

- the bundle goes to `<rootdir>/.coverset` (pytest's rootdir, so a run from `tests/` writes to the same place);
- the measured packages come from `[tool.coverset]` in `<rootdir>/pyproject.toml`: `source` (a module name or a list of them), else the top-level packages found under `source_root` (default `src` when it is a directory, else `.`), minus `tests`, `docs` and the like, which is how the control plane infers them. A malformed table, or nothing to measure, is a usage error (exit 4) that says to set `source` or pass `--coverset-source`;
- the neutral export is on;
- one line is printed after the bundle is written, ``coverset: wrote .coverset (24 tests, 4 files); `coverset report .coverset` for the pages`` (the directory relative to the working directory, the tests that ran, the files measured), and nothing else: the runner's JSON event lines ([Events on stdout](#events-on-stdout)) are not written in a local run, even when `COVERSET_EVENT_NONCE` happens to be set (`test_coverset_prints_one_line_and_no_events`). Each entry of `meta.warnings` is appended to the line as `; warning: ...`; today there is one, for sources that measured nothing: `[tool.coverset] source = ["does_not_exist"]` (or a misspelt `--coverset-source`) still runs the tests, and the line ends `(1 test, 0 files); ...; warning: no file of the measured sources (does_not_exist) was found: check [tool.coverset] source or --coverset-source` instead of a silent "0 files" (`test_sources_that_measure_nothing_are_a_warning`).

The plugin removes `COVERSET` from the environment as soon as pytest starts, before conftest files load and whether or not `--coverset` is passed as well, so pytest runs the tests start themselves (subprocesses, `pytester`) are not instrumented as well (`test_coverset_variable_never_reaches_subprocesses`, both forms). The runner never uses the switch: `--coverset-out` keeps its meaning, reads no `pyproject.toml`, writes the neutral bundle only on request and prints nothing extra; without `--coverset-source` it measures all in-repo code (`test_out_without_sources_still_measures_in_repo_code`). Tests: `packages/pytest-coverset/tests/test_local.py`.

The output directory must not be a symlink (even one to a directory) or a file, and no directory between the repository root (the git root, else the rootdir) and it may be a symlink either, so a link committed to the repository (`build -> /elsewhere` with `--coverset-out=build/out`, or `link/../out`) cannot redirect the bundle: each is a usage error before anything is written (`test_a_symlinked_output_directory_is_refused`, `test_an_output_directory_below_a_symlink_is_refused`). The path is checked as written, component by component below the root; links above the root (macOS `/tmp`) do not count, and an output directory outside the repository is checked by its own name only (`test_an_output_directory_outside_the_repository_may_sit_below_a_link`). A directory the plugin creates gets a `.gitignore` holding `*`, so a bundle is never committed by accident; a directory that already existed is left as it is (`test_gitignore_only_in_a_directory_the_plugin_created`).

| Option | Default | Effect |
|---|---|---|
| `--coverset` | off | turn instrumentation on with the local defaults above; `COVERSET=1` does the same |
| `--coverset-out DIR` | off (`<rootdir>/.coverset` with `--coverset`) | write the bundle to `DIR`; turns instrumentation on |
| `--coverset-neutral` | off (on with `--coverset`) | also write the language-neutral bundle, `DIR/neutral/` and `DIR/neutral.tar.gz`; `COVERSET_NEUTRAL=1` in the environment does the same ([The neutral bundle](#the-neutral-bundle)) |
| `--coverset-scores FILE` | off | JSON `{nodeid: score}`; run the riskiest tests first |
| `--coverset-source PKG` | all in-repo code (`[tool.coverset]` with `--coverset`) | package name or path to measure and trace (repeatable) |
| `--coverset-trace-module MOD` | none | extra module, and its submodules, to trace (repeatable) |
| `--coverset-full-trace FILE` | none | repo-relative paths, one per line, traced without a sample cap |
| `--coverset-shapes on\|off` | `on` | record argument and return shapes |
| `--coverset-repr-examples off\|scalars\|all` | `scalars` | which redacted example values to keep |
| `--coverset-stop-file PATH` | none | once `PATH` exists, skip the remaining tests, write the bundle, exit 2 |
| `--coverset-shape-samples N` | 2048 | sampled calls per function before sampling stops |
| `--coverset-shape-settle N` | 64 | consecutive calls with no new shape before sampling stops |
| `--coverset-full-trace-cap N` | 100000 | safety cap on sampled calls per full-trace function ([below](#adaptive-sampling-and-the-full-trace-list)) |

Every option accepts both `--opt value` and `--opt=value` (`test_option_values_in_both_forms`), with one exception, below: a separate value word that names an existing path and moves pytest's rootdir or configuration file stops the run.

The orchestrator builds the command line through `PythonAdapter.test_command` (`src/coverset/lang/python.py`). It always adds `--continue-on-collection-errors -p no:cacheprovider -q`, plus `--numprocesses=0` when `pytest-xdist` is in the environment and `--no-cov` when `pytest-cov` is.

### Value options are one word: `--opt=value`

`test_command` writes every option that takes a value as a single word: `--coverset-out=run/out`, never `--coverset-out run/out`. pytest looks for its configuration file (`_initini`) before entry-point plugins have registered their options. At that point `--coverset-out` is an unknown flag and the following word is taken for a positional argument; when that word is an existing path, pytest uses it to infer the rootdir. In a worker the scores file, the full-trace list and the output directory live in the sibling `run/` directory, so the inferred rootdir became the common ancestor of `repo/` and `run/`, no `pyproject.toml` or `pytest.ini` was found there, and `--rootdir=` suppressed the fallback search. Every setting of the target repository was then silently dropped in every shard: `xfail_strict`, `addopts`, `markers`, `pythonpath`, `filterwarnings`, `asyncio_mode`, plugin settings such as `DJANGO_SETTINGS_MODULE`. A repository with `xfail_strict = true` and a passing xfail ran `1 xpassed`, exit 0, under the old argv and `1 failed`, exit 1, under plain pytest and the `=` form. `--opt=value` words start with `-`, so the early lookup ignores them.

The plugin guards a hand-typed command line the same way. In its first hook it looks for a `--coverset-* VALUE` pair (on the command line or in `PYTEST_ADDOPTS`) whose value is an existing path, asks pytest's own `determine_setup` where the rootdir and configuration file would be without those words, and stops (exit 4) when the answer differs, naming the one-word form: `pytest --coverset-out ../out` run a second time, when `../out` exists, would otherwise lose the project's `pyproject.toml`. A value inside the project (`--coverset-out .coverset`) changes nothing and passes. An xdist controller also hands its workers every pair as one word, on their command line and in `PYTEST_ADDOPTS`: the workers parse the command line again after the controller has created the output directory (`test_a_separate_value_word_keeps_the_workers_configuration`).

Tests: `test_ini_options_survive_the_shard_argv` (`tests/analysis/test_analysis_test_command.py`) runs the exact shard argv against a worker-shaped layout and asserts exit 1 and `configfile: pyproject.toml`; `test_the_split_form_is_refused` is the control (the `--opt value` form stops with exit 4). `meta.json` records the configuration file pytest actually used as `configfile` (`null` when none was found).

## Per-test coverage

### Why the ctrace core

coverage.py has two tracer cores that matter here: the C `settrace` tracer (`ctrace`) and the `sys.monitoring` core (`sysmon`). Only ctrace supports dynamic contexts. coverage.py's own core selection says so: with dynamic contexts requested, `coverage/core.py` refuses sysmon with the reason "it doesn't yet support dynamic contexts". With branch coverage on, the same function also refuses sysmon before Python 3.14 ("sys.monitoring can't measure branches in this version"). On 3.14 sysmon becomes the default, so the core must be chosen explicitly.

`CoverageContext` (`coverage_ctx.py`) therefore builds `coverage.Coverage(data_file=<out>/coverage.sqlite, data_suffix=False, branch=True, source=<sources or None>, config_file=True)` and, before `start()`, sets:

| Option | Value | Why |
|---|---|---|
| `run:core` | `ctrace` | dynamic contexts (the constructor has no `core=` argument) |
| `run:relative_files` | `True` | names relative to the working directory; the plugin makes them repository-relative when the run ends ([Paths](#paths)) |
| `run:dynamic_context` | `None` | the plugin switches contexts itself |
| `run:context` | `None` | no static context prefix, so a context is exactly a node id |
| `run:parallel` | `False` | one data file per shard |
| `run:disable_warnings` | five slugs | `module-not-imported` and friends are expected when a shard imports part of the sources |

`meta.json` records the tracer coverage.py actually used (`"tracer": "CTracer"` in `test_bundle_contents_and_schemas`) and `"core": "ctrace"`.

coverage.py warnings are captured around every call, never raised, so a target repository that runs with `-W error` or `filterwarnings = error` keeps working (`test_strict_warnings_do_not_break_the_run`). The expected slugs above are disabled outright; any other `CoverageWarning` lands in `meta.json` `coverage_warnings` (at most 50) instead of being thrown away (`test_coverage_warnings_are_captured_not_raised`).

### One context switch per test

Coverage starts in `pytest_load_initial_conftests`, before conftest files are imported, so their import-time lines are measured. A `pytest_runtest_protocol` hook wrapper calls `switch_context(item.nodeid)` once per test. Setup, call and teardown all land in the test's context. That is a deliberate simplification: a fixture's lines count for the test that requested it.

Code that runs before the first test (imports, collection) stays in coverage.py's `''` context. The bundle reader maps `''` to the pseudo test id `<import>` (`facts/convert.py`). Test `test_one_context_per_test` checks that the recorded contexts are exactly `''` plus one per node id, including ids that contain `|`.

### When the tracer is lost

A test that calls `sys.settrace(None)`, or installs its own trace function and does not restore coverage's, used to stop measurement for the rest of the shard with `meta.errors == []` and exit 0. Before every context switch (and once more at session end) `CoverageContext.check_tracer` compares `sys.gettrace()` with the collector's tracer objects, by identity. On a mismatch:

| Recorded | Where |
|---|---|
| the context that was running when the tracer went missing (the culprit test; `<import>` before the first test) | `meta.json` `tracer_lost_at` (the first loss) |
| each loss as `{context, found, reinstalled}` (`found` is the trace function's type, or `none`) | `meta.json` `tracer_losses` (at most 20) |
| one sentence naming the test, what replaced the tracer and whether it was re-installed | `meta.json` `errors` |

The tracer is then re-installed through coverage.py's public `Coverage.stop()` and `Coverage.start()`, so only the culprit test loses lines; the following tests are measured again (`test_lost_tracer_is_recorded_and_reinstalled`). This is done only while the plugin's Coverage object is the active one. When another Coverage object was started on top of it, which is what pytest-cov does without `--no-cov`, `stop()` would fail coverage.py's collector-stack assertion, so the loss is recorded with `reinstalled: false` and nothing is touched (`test_a_coverage_object_stacked_on_top_is_recorded`). coverage.py's own "Trace function changed" warning comes only from its Python tracer, never from ctrace, which is why the plugin checks itself.

The control plane reads `tracer_lost_at`: `facts/convert.py` (`tracer_mismatch`) flags the shard as a `tracer_mismatch` with the text "CTracer lost at <context>" (printable, at most 64 characters), ingest records it in the shard's `ingest_log`, and `orchestrator/runner.py` (`tracer_warning`) writes a `log` event "coverage lost its tracer at <nodeid>; per-test contexts after it may be incomplete" (`tests/facts/test_facts_fixes.py::test_a_lost_tracer_is_a_tracer_mismatch`, `tests/orchestrator/test_review_fixes.py::test_a_lost_tracer_reaches_the_run_events`).

### When data reaches disk

coverage.py hands the current context's data to SQLite on every `switch_context`. It used to connect to the file, set two pragmas, register a function, insert, commit and close the connection for every one of those writes: 177 µs per test, 0.9 s of a 5,000-test run, most of it connecting (`coverage_ctx.py`'s docstring has the measurement). The plugin now keeps that connection open from the first write that has data (it enters coverage.py's own `SqliteDb` context once, so its nesting counter skips every inner connect and close) and commits at most once a second, plus at once after a `failed` or `error` result (`cov.get_data()`, then a commit): 25 µs per switch. In a serial run a SIGKILL therefore loses at most a second of per-test lines, which it loses anyway together with the in-memory shapes and the bundle (in a serial run nothing reads `coverage.sqlite` before this process packs the bundle). An xdist worker is the exception: when it dies, the controller still merges its data file, next to results that say its earlier tests passed. A worker therefore commits at every switch (`CoverageContext.commit_interval_s = 0`), which keeps the connection held and adds about 19 µs per switch (32 µs to 51 µs in the same microbenchmark on a loaded machine; coverage.py's `journal_mode=off` and `synchronous=off` make a commit cheap and fsync-free); only the test that was running when the worker died loses its lines. The hold uses two private names of coverage.py (`CoverageData._connect`, `SqliteDb.nest`); without them nothing is held and every write behaves as before. At session end the plugin releases the connection, stops, saves, and writes `coverage.json` with `cov.json_report()` (format 3). `meta.timings.context_switches` and `context_switch_s` say how many switches the run made and what they cost in all.

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
| 4 | usage error: a foreign tracer, remote xdist workers, Python below 3.12, an unreadable `--coverset-scores` file, an output directory that is a symlink, lies below a symlink inside the repository, or is a file, `--coverset` with nothing to measure, or a conftest that fails to import |
| 5 | no tests collected |

The runner treats 0, 1 and 5 as normal shard ends, and 2 as normal only after it asked for a stop (`runner._shard_error`).

When pytest gives up before the test session starts, the plugin still finalises. A conftest that fails to import makes pytest exit 4 without ever calling `pytest_sessionfinish` or `pytest_unconfigure`; a `pytest_load_initial_conftests` wrapper catches that, writes the bundle with the error in `meta.errors`, and emits `bundle_written` and `done` with `exit_code` 4 (`test_conftest_import_error_still_writes_the_bundle`). A usage error raised after the session started (an unreadable scores file) is finalised the same way from `pytest_unconfigure` (`test_unreadable_scores_file_still_writes_the_bundle`). Every one of these paths (normal end, STOP file, SIGTERM, a failed conftest, a late usage error) goes through the same `finalize`, so with `--coverset-neutral` each also writes the neutral bundle and emits `neutral_written` before `done` (`test_early_exits_still_write_the_neutral_bundle`, `test_stopped_run_lists_not_run_tests_with_a_null_outcome`). The neutral export runs after `meta.json` and `bundle.tar.gz` are written and `bundle_written` is out, so a Ctrl-C during it (1.1 to 1.4 s on an 18,000-function module) leaves the Python bundle complete and `meta.json` saying `"neutral": {"status": "interrupted"}` (`test_an_interrupt_during_the_export_keeps_the_python_bundle`).

## The shape tracer

`ShapeTracer` (`shapes.py`) records which kinds of value flowed into and out of each traced function. It is a second `sys.monitoring` tool that coexists with coverage's ctrace core.

### Tool ids

CPython gives `sys.monitoring` tools ids 0 to 5 (asking for 7 raises "invalid tool 7 (must be between 0 and 5)"); `sys.settrace`, which ctrace uses, is implemented on reserved ids outside that range. coverset tries ids in the order 4, 5, 2 and records the one it got as `meta.json` `tool_id`. It avoids 1 (`sys.monitoring.COVERAGE_ID`, which coverage's sysmon core claims) and 3 (Hypothesis's scrutineer: `hypothesis/internal/scrutineer.py` sets `MONITORING_TOOL_ID = 3`). If none of 4, 5 and 2 is free, shapes are off for the session and `shapes_disabled_reason` names the owners (`test_tool_id_fallback`).

### Discovery: a global `PY_START`, then local events

1. The tracer enables one global event: `PY_START`.
2. The first `PY_START` of each code object decides, once, whether it is a target. Targets are remembered (their sampling state holds the code object).
3. A non-target returns `sys.monitoring.DISABLE`, so CPython stops reporting that code location to this tool, and is not remembered: code compiled at run time is freed as usual.
4. A target gets local events with `set_local_events(tool_id, code, PY_START | PY_RETURN)`. Generators and async generators get `PY_START` only.

A code object is a target when all of these hold:

- its file is inside the repository (`norm_path` against the git root);
- the file is not a test module or conftest (`test_*.py`, `*_test.py`, `conftest.py`);
- it is a function body (`CO_OPTIMIZED`), not module or class body code, and not a `<genexpr>`-style code object (`<lambda>` is allowed);
- its file is in the full-trace list, or it matches `--coverset-source` (a package name matched against the module's `__name__`, or a path) or `--coverset-trace-module`. With none of those given, every in-repo file outside virtualenv directories matches.

`restart_events()` is never called: it is global across tools and would undo other tools' `DISABLE` decisions.

### Adaptive sampling and the full-trace list

Sampling is adaptive per code object per session. A function stops being sampled once 64 consecutive calls (`--coverset-shape-settle`) produced no new `(parameter, shape)` pair, or once 2,048 calls (`--coverset-shape-samples`) were sampled. The tracer then switches the code object's local events off, returns `DISABLE`, and marks the function truncated. Every observation row of that function carries `trunc: true`, and its `func_sampling` row says `truncated: true`.

Functions in files on the full-trace list skip the adaptive cap. The orchestrator writes that list into each worker as `run/full-trace.txt`: the PR's changed source files plus the file of every invariant target (`orchestrator/planner.py`). Test `test_adaptive_cap_and_full_trace` exercises both: a hot function stops after more than 64 and fewer than 80 calls, while its full-trace twin records all 300.

Full-trace functions still have a high safety cap: 100,000 sampled calls per code object (`--coverset-full-trace-cap`). Without it a changed function called from a tight loop cost every call a Python callback: a 3-million-call loop ran 12.2× slower than plain pytest on 3.12 and 10.5× on 3.13 (see the table under [Overhead](#overhead-on-tight-loops)). A function that reaches the cap is truncated like any other: `trunc: true` on its rows, `truncated: true` on its `func_sampling` row, which also says why with `cap: "full"` (the adaptive cap is `cap: "sample"`, an uncapped function has `cap: null`). The input-class rule therefore still holds: a class missing from a capped function reads "not observed in N sampled calls", never "never exercised" (`test_full_trace_functions_stop_at_the_safety_cap`).

Why truncation matters downstream: a class missing from a truncated function is reported as "not observed in N sampled calls", never as "never exercised" ([docs/input-classes.md](input-classes.md#never-exercised-or-not-observed)).

### What a sample reads

On `PY_START` the callback gets `(code, offset)`. It fetches the callee frame with `sys._getframe(1)` and reads only the parameters from `frame.f_locals`: `co_varnames[:co_argcount + co_kwonlyargcount]`, then `*args` and `**kwargs` when `co_flags` says they exist. Varargs keep their real names with the stars (`*args`, `**kw`).

On Python 3.12 `frame.f_locals` is a dict snapshot cached on the frame; left filled it would keep every argument alive until the function returns, so a `del arg` inside the function would not free it. The tracer clears the snapshot once it has read the parameters. Nothing in the function has run yet at `PY_START`, and any later `f_locals` or `locals()` refreshes it, so the clear is invisible to the code under test. On 3.13 `f_locals` is a write-through proxy and nothing is cached (`test_arguments_die_when_the_function_deletes_them`, run on both versions).

For a method's `self` or `cls` only the receiver's class is recorded, under the pseudo-parameter `<receiver>`. A first parameter named `self` or `cls` counts as a receiver only when the function is defined directly in a class body (its `co_qualname` parent is a class, not `<locals>`) and the value's class, or the value itself for `cls`, has a class of that name in its `__mro__`. A nested `def inner(self, x)` and a `@staticmethod def parse(self)` therefore record `self` as an ordinary parameter with its full shape (`test_receivers_need_an_owner_class_and_callers_skip_generator_frames`).

On `PY_RETURN` the callback gets the return value directly (`retval`) and records its shape under the pseudo-parameter `return`.

Each observation also records its caller. When the calling frame belongs to an in-repo, non-test function, the caller is that function's `(path, co_qualname, co_firstlineno)`. Generator-expression frames (and, before 3.12 inlined them, comprehension frames) are skipped, up to 8 frames out, so `sum(helper(i) for i in items)` inside `gen_caller` names `gen_caller`. Module and class bodies (a call made at import time) are never callers. Otherwise the caller is the running test's node id, or `<import>` outside any test. The flow ribbon is drawn from these callers.

Raised exceptions are not recorded: `PY_UNWIND` can only be enabled globally, not per code object.

### Safety rules

The tracer runs inside the code under test, on every traced call. It must never change behaviour.

| Rule | How |
|---|---|
| No re-entrancy | a thread-local `busy` flag; a callback that fires inside a callback returns at once |
| Never raise into user code | the callback body is wrapped in `try/except BaseException`; errors are counted in `meta.json` `tracer_errors`. `KeyboardInterrupt` alone propagates, so a SIGTERM stop is not swallowed. |
| One bad value does not drop the call | each parameter is observed in its own `try` |
| No user code runs | features are computed only for values whose type is exactly `NoneType`, `bool`, `int`, `float`, `str`, `bytes`, `list`, `tuple`, `dict`, `set` or `frozenset`. User `__len__`, `__iter__`, `__bool__`, `__eq__`, `__hash__` and `__repr__` are never called (`test_user_dunders_are_never_called`). Classes are compared by identity (`id(cls)` against a set of builtin ids, `cls is int`), never by `in` on a set or tuple, so a metaclass `__hash__` or `__eq__` is never called; class names and `__module__` are read through `type`'s own descriptors and used only when they are exact `str` (`test_metaclass_dunders_are_never_called`). |
| Bounded work | containers are summarised over at most 32 leading elements, read by index (`list`, `tuple`) or builtin iteration (`set`, `dict`); string kinds are skipped above 2,048 characters; whitespace and digit features scan at most 4,096 characters; a sum that overflows (a huge int next to a float) drops only the `sum_zero` feature |
| No objects kept | the aggregate holds shape keys, counts, test indexes and redacted example strings, never the values; the 3.12 `f_locals` snapshot is cleared after each sample |
| No code objects kept | only target code objects are remembered; a non-target gets `DISABLE` and is forgotten, and pseudo file names such as `<string>` are not cached, so code generated at run time is freed (`test_non_target_code_objects_are_not_retained`; 100,000 `exec`-compiled functions peaked at 76 MB RSS, down from 274 MB) |
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
2. Every string, top-level or inside a container, is scrubbed *before* it is shortened: credential patterns as in the control plane's `coverset.logging` (PEM blocks, `x-access-token:...@`, `gh[opsur]_...`, `github_pat_...`, `sk-ant-...`, `Authorization` headers), and every email keeps its domain but loses its local part (`bob@eng.acme.com` becomes `b***@eng.acme.com`). A string longer than 8,192 characters is scrubbed as its first and last 4,096 characters, the only parts that can be shown, unless it contains a PEM private key, which is scrubbed whole.
3. A `reprlib.Repr` with `maxstring=48, maxother=48, maxlevel=2` formats exact builtin types only; anything else prints as `<module.Class>` without calling its `__repr__`. Ints wider than 128 bits print as `<int N bits>`.
4. The finished text is scrubbed once more.
5. The result is cut to 200 characters.

Scrubbing only after reprlib's cut, as earlier versions did, leaked what the cut separated from its prefix: `'sk-ant-[REDACTED]...YZabcdefghijklmnop end'`, `'please email johnsmit...ample.com'`, and the tail of a Bearer token. They now come out as `'sk-ant-[REDACTED] end'`, `'please email j***@corp.example.com about it'` and `'Authorization: Bearer [REDACTED] tail'` (`test_secrets_are_scrubbed_before_reprlib_shortens_the_text`).

Tests: `test_example_redaction`, `test_secrets_are_scrubbed_before_reprlib_shortens_the_text`, `test_return_values_of_secret_named_functions_are_redacted`, `test_secret_dict_keys_are_redacted_in_container_examples`, `test_huge_ints_and_long_bytes_are_summarised`, `test_hostile_values_keep_every_parameter_and_hide_secrets`.

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
| nonce | a fresh 128-bit hex value per exec in `COVERSET_EVENT_NONCE`. Without it no event is written at all, and none is with `--coverset`/`COVERSET=1` either: events are for the orchestrator, which always sets the nonce and never uses the local switch (`test_no_nonce_no_events`, `test_coverset_prints_one_line_and_no_events`) |
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
| `neutral_written` | `path`, `sha256`, `bytes` of `neutral.tar.gz`, plus `seconds` (time spent exporting), `rows` per file and `dropped` (counts of rows left out, below); only with `--coverset-neutral`, after `bundle_written`, and only when every file, the manifest and the archive were written (`meta.json`'s `neutral` says what happened otherwise) |
| `done` | `exit_code`, `counts` (every outcome plus `not_run`) |

`outcome` is one of `passed`, `failed` (the body failed, including a strict XPASS), `error` (setup or teardown failed), `skipped`, `xfailed`, `xpassed`. `fail_sig` is the blake2b-8 hex digest of the exception type plus the crash location, so the same failure on two commits gets the same signature even when its message differs (`test_fail_sig_is_stable_across_messages`). `longrepr` is cut to 8 KiB of UTF-8 with `truncated` set. The skip or xfail `reason` is cut to the same 8 KiB, silently (a 2 MB `pytest.skip()` message once produced a 2,000,292-byte event line, over the executor's 1 MiB line cap; it is now 8,484 bytes).

Surrogates: a lone surrogate (`"\udcff"`, from a `surrogateescape`d file name or a hand-built message) in a failure message, a skip or xfail reason or a collection error becomes `?` when the text is cut, so `longrepr`, `reason` and the collection error's `longrepr` are always valid UTF-8. Before, it raised `UnicodeEncodeError` in the results writer: the row and its `test` event were lost, and a module raising one at import left `collect.json`, `durations.json` and `invariant_links.jsonl` out of the tarball with a `collect.json.tmp` behind. The `test` event is now emitted even if the `results.jsonl` write fails (the error goes to `meta.errors`), every JSON file in the bundle is written ASCII-only (`ensure_ascii=True`), each of the three static files is written independently, and no `.tmp` file survives a failed write (`test_lone_surrogates_and_huge_reasons_keep_every_result`).

## The bundle

At session end the plugin writes every file below into `--coverset-out`, then packs them into `bundle.tar.gz`. The tarball is flat, streamed to disk, renamed into place atomically, and has owner ids and names cleared. Its sha256 and size go out in the `bundle_written` event. An error while writing the coverage, shapes or static files is recorded in `meta.json` `errors`, and the remaining files are still written.

### Paths

Every path is repository-relative: POSIX, relative to the git root (the rootdir when there is no `.git`), whichever directory pytest runs in. `meta.cwd` and `meta.rootdir` say where the run happened, relative to the same root (`.` for the root itself; `null` for a directory outside it). `paths.py` holds the one normaliser, so coverage file keys, tracer function keys, `git diff` paths and AST indexes join without translation (`test_paths_agree_between_coverage_and_shapes`).

| Where | How the path is made |
|---|---|
| `shapes.jsonl.gz` function paths, `results.jsonl` and `collect.json` `file`, the neutral `functions` and `tests` | `norm_path` of the code object's file or the test item's path, relative to the git root |
| `coverage.json` keys, `coverage.sqlite`'s `file` table | coverage.py (`relative_files`) names a file under the working directory relative to it and any other file absolute. When the run ends, after `coverage.json` is written, each name is resolved against the working directory and made git-root-relative (`paths.coverage_name`): `coverage.json`'s keys are rewritten, and `coverage.sqlite` is rebuilt through coverage.py's public `CoverageData.update(..., map_path=)`, the call `coverage combine` uses for `[paths]`. Both are written under temporary names and renamed into place, and nothing is rebuilt when no name changes (a run from the git root, as in a worker). A file outside the repository keeps its absolute name, which every reader drops and counts. `meta.coverage_paths` is `repository` once this is done; if it failed (recorded in `meta.errors` as `coverage paths`), it is `cwd` and the names are still coverage.py's. Those are relative only under the working directory: when pytest ran from a directory that does not contain the sources (from `tests/`, with the code in `src/`), every source name is still absolute, every reader drops it, and the coverage of that run is lost; the bundle stays readable (results, shapes, collection) and `meta.errors` says why |
| test node ids | unchanged: pytest's, relative to the rootdir. They are test identities (coverage contexts, scores, args files), not paths |

Readers apply one rule (`coverset.facts.convert.read_python_bundle` and the neutral export alike): with `meta.coverage_paths == "repository"` names are taken as they are; otherwise `meta.cwd` is joined in front of each relative coverage name, and a test file taken from a node id gets `meta.rootdir` in front. Bundles written before these keys existed recorded absolute `cwd` and `rootdir`, which are not relative paths, so nothing is joined and they read exactly as they always did. `tests/plugin_conformance/test_neutral_conformance.py` runs the fixture project from its `tests/` directory and a package whose rootdir sits below the git root, and requires every path to be the git-root-relative one in both readings; it also forces the rewrite to fail and checks both readers still agree, and reads an old-style bundle as before.

| File | Schema |
|---|---|
| `coverage.sqlite` | coverage.py's SQLite data file, schema 7: tables `file`, `context`, `line_bits` (numbits blobs), `arc` (with branch coverage on, lines are the positive arc endpoints). One context per node id plus `''`. |
| `coverage.json` | `cov.json_report()` format 3. coverset reads `meta.format`, and per file `executed_lines`, `missing_lines`, `excluded_lines`, `executed_branches`, `missing_branches`. The report also carries `functions` and `classes` regions (`test_coverage_json_fields`). |
| `shapes.jsonl.gz` | gzip JSON lines: one header, then rows (below). Header-only when shapes are off. |
| `results.jsonl` | one row per finished test with exactly the `test` event fields; written as tests finish and authoritative over streamed events |
| `collect.json` | `{"items": [{nodeid, file, line, markers, invariant_ids, hypothesis}], "errors": [{nodeid, longrepr, truncated}]}`. `line` is 1-based (the first decorator line). `hypothesis` is true for `@given` tests. |
| `durations.json` | `{nodeid: duration_s}` |
| `invariant_links.jsonl` | `{"test_id": <nodeid>, "invariant_id": <id>}` per marker link |
| `meta.json` | `schema`, `plugin_version`, `python`, `coverage_version`, `pytest_version`, `core`, `tracer`, `tracer_lost_at`, `tracer_losses`, `coverage_warnings`, `context_format`, `tool_id`, `git_root`, `repo_root` (both absolute), `rootdir` and `cwd` (relative to `repo_root`, [Paths](#paths)), `coverage_paths` (`repository` or `cwd`), `configfile`, `sources`, `trace_modules`, `full_trace`, `shapes`, `shapes_disabled_reason`, `repr_examples`, `shape_samples`, `shape_settle`, `full_trace_cap`, `neutral` (`{"requested": false}` without `--coverset-neutral`; with it, the export's outcome: `status` `written`, `failed` or `interrupted`, `rows`, `dropped`, `seconds`, and `path`, `sha256`, `bytes` of the archive when written; the copy inside `bundle.tar.gz`, packed before the export, says `pending`), `functions_traced`, `functions_truncated`, `tracer_errors`, `hypothesis` (`version`, `deadline_ms`), `exit_code`, `counts`, `stop_requested`, `errors`, `warnings` (advice that is not an error, such as sources that measured no file; printed on `--coverset`'s summary line), `started_at`, `finished_at`, `timings` (`session_wall_s`, `tracer_callback_s`, `tracer_events`, `context_switches`, `context_switch_s`, and `finalize`: seconds per finalisation step, `coverage_save`, `coverage_json`, `coverage_paths`, `shapes`, `static_files`, `merge` under xdist, and `tarball` in the copy rewritten after the neutral export), `xdist` (`null` for a run without workers; [Running in parallel](#running-in-parallel)) |

With `--coverset-neutral` the directory also holds `neutral/` and `neutral.tar.gz` ([below](#the-neutral-bundle)); neither is a member of `bundle.tar.gz`, whose eight members are unchanged.

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
{"kind": "func_sampling", "f": 0, "calls": 213, "tests": [0, 1, 2, ...], "truncated": false, "cap": null}
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
| `func_sampling` | one row per traced function: sampled `calls`, sampled `tests`, `truncated`, and `cap` (`"sample"`, `"full"` or `null`: which cap stopped sampling) |

The block above is an excerpt of the bundle from the hand run described in [Running it by hand](#running-it-by-hand), with the lists shortened (`...`). A caller of kind `func` has the form `{"kind": "func", "path": ..., "qualname": ..., "line": ...}`.

## The neutral bundle

`--coverset-neutral` (or `COVERSET_NEUTRAL=1`, so the orchestrator can switch it on without changing the shard argv) makes the plugin also write the language-neutral bundle that [docs/neutral-bundle.md](neutral-bundle.md) specifies, the same format a TypeScript reporter writes. It is off by default. `src/pytest_coverset/neutral.py` writes it.

| File in `DIR/neutral/` | Made from |
|---|---|
| `manifest.json` | `format` `coverset-neutral`, `version` 1, `language` `python`, `producer` `{"name": "pytest-coverset", "version": <plugin version>}`, `created_at` (UTC), `shard` from `COVERSET_SHARD` (an integer, else `null`), `meta` = the mapping written to `meta.json`, except that its `neutral` holds the data files' `rows` and `dropped` (the archive does not exist yet); written only when every data file was |
| `tests.jsonl.gz` | every collected test in collection order, merged with its final result; `outcome` `null` for a test that never ran (STOP file, SIGTERM); `message` is the result's `longrepr` (already cut to 8 KiB); `markers`, `invariants` and `property` (Hypothesis) from the collected item; `start`, `stop`, `duration_s` from the result. A test id longer than 512 characters (the format's limit) is left out here and in every other file and counted (`tests_long_ids`, `coverage_long_test_ids`, `shapes_long_test_ids`), so a reader never drops a whole row for it |
| `files.jsonl.gz` | `coverage.json` per file: `statements` = executed ∪ missing lines, `excluded`, `branches` = executed ∪ missing branch arcs; `n_lines` counted from the source file as Python counts lines (`\n`, `\r\n` and a lone `\r` each end one, as in the control plane's converter) |
| `coverage.jsonl.gz` | `coverage.sqlite` through coverage.py's public `CoverageData` API: `contexts_by_lineno` finds the (context, file) pairs, then `set_query_context` plus `arcs()` per pair (`lines()` is never called: the plugin always measures branches, and data without arcs would fall back to the line numbers `contexts_by_lineno` returned). Raw tracer lines are mapped to their statement's first line exactly as the control plane's `read_python_bundle` maps them, and only statement lines are kept (a module docstring's line 1, recorded at import, is not a statement). The `''` context is `<import>`. Each row carries the pair's `arcs`. A file the data measured but `coverage.json` does not list (coverage.py could not report it, e.g. its source was deleted during the run) keeps its raw executed lines and gets no `files.jsonl` row, which the format allows (`coverage_files_not_in_files` counts them); the converter keeps those lines too. |
| `arcs.jsonl.gz` | `coverage.json`'s executed branch arcs per file (the run-wide union) |
| `functions.jsonl.gz` | the function regions of every measured `.py` file, without `types` (below); each source is decoded as CPython decodes it (a UTF-8 BOM or a PEP 263 coding cookie, as the control plane's planner does), and a file that cannot be read or indexed (nested too deep for the AST walk) is skipped and counted (`functions_unreadable_sources`, `functions_unindexable_sources`) without losing the other files' regions |
| `shapes.jsonl.gz` | the plugin's header-indexed `shapes.jsonl.gz` rewritten as self-contained `obs` rows (`func`, `param`, `shape`, `caller`, `samples`, `tests`, `truncated`, `example`, `example_test`) and `sampling` rows (`func`, `calls`, `tests`, `truncated`, `cap`). Observations whose caller is `<import>` (a decorator applied at import time) are left out, as the control plane's Python reader leaves them out. |
| `links.jsonl` | `invariant_links.jsonl` as `{"test", "invariant"}` |

Test files, shape paths and `coverage.json`/`coverage.sqlite` file names are repository-relative by the time the export reads them ([Paths](#paths)), from any working directory; a name outside the repository is absolute, which the format does not allow, so it is dropped and counted (`files_dropped`, `coverage_files_not_relative`). Should the session have failed to rewrite the coverage names (`meta.coverage_paths == "cwd"`), the export joins `meta.cwd` in front of each relative name, exactly as the converter does, so the two readings still agree. Every JSON line is UTF-8 (a lone surrogate becomes `?`), every `.gz` has a zero mtime, and each file is written to a temporary name and renamed. `DIR/neutral.tar.gz` holds the same eight files (the manifest and seven data files) as flat members (owner ids and names cleared), next to `bundle.tar.gz`; its sha256 and size go out in `neutral_written`.

**Order and failures.** `finalize` writes `meta.json`, packs `bundle.tar.gz` and emits `bundle_written` first; only then does the export start. It removes what an earlier export left (`DIR/neutral.tar.gz`, the manifest and both variants of every file, so a stale `tests.jsonl` never sits next to a new `tests.jsonl.gz`), writes the seven data files (a failure in one does not stop the others), then the manifest only if every data file was written, then the archive only if the manifest was. `meta.json` is rewritten at the end with `neutral.status`: `written` (and `neutral_written` emitted), `failed` (each error in `meta.errors` under `neutral`, `neutral manifest` or `neutral tarball`; no manifest over incomplete data, no archive, no event) or `interrupted`. A directory or a file where the export needs to write (`DIR/neutral.tar.gz/`, `DIR/neutral` as a file, a read-only `DIR/neutral/`, a directory named `tests.jsonl.gz`) is such a failure (`test_a_failed_export_is_recorded_and_never_announced`). Without the flag, a neutral bundle an earlier run left in `DIR` is removed.

**Equivalence.** The export only reads files the session already wrote (`coverage.sqlite`, `coverage.json`, `shapes.jsonl.gz`) and rows already recorded, after measurement stopped, so it cannot change coverage, shapes or results. `test_the_export_changes_nothing_else` runs the fixture project with and without the flag and compares `results.jsonl` (all but the timing fields), every `(context, file, from, to)` row of `coverage.sqlite`, the decoded shapes header and rows, `collect.json`, `invariant_links.jsonl` and `coverage.json` (all but its timestamp): identical. In the workspace, `tests/plugin_conformance/test_neutral_conformance.py` reads the neutral bundle with `coverset_model.io.read_bundle` and the Python bundle of the same run with `coverset.facts.convert.read_python_bundle`, and requires the two `NeutralBundle`s to be equal dataclass by dataclass (tests, collected items, durations, files, run arcs, coverage, shapes, sampling, `meta`), for the fixture project, for a project with every outcome and tests a STOP file kept from running, for the fixture project run from its `tests/` directory, for a git repository whose package and rootdir are a subdirectory, and for that repository with the coverage-name rewrite forced to fail. One documented difference is applied to the converter's side: it keeps non-statement lines, which the neutral format drops. Both readers carry the sampling `cap`, so sampling rows compare as they are. `meta.neutral` is compared apart: `meta.json` has the export's final outcome, the manifest the data files' counters. `coverset_model.io.validate` finds no problem in the directory or the archive, and the archive reads back exactly as the directory.

**Cost.** `pytest_coverset.neutral` is imported whether or not the flag is on, so the flag costs the export itself and nothing else. The export (the `seconds` in `neutral_written`) takes 0.013 s on the plugin fixture project (24 tests, `--coverset-source fixturepkg`, Python 3.13, this Mac, 2026-10-11; 0.0126 to 0.0133 s over 8 runs). The median wall time of interleaved runs with and without the flag differs by about that plus run-to-run noise: +0.011 s and +0.003 s in two series of 8 runs, +0.018 s in a third (0.392 s without, 0.410 s with). On a copy of `examples/demo-app` (97 tests) the export took 0.055 s; on a module of 18,000 functions, 1.1 to 1.4 s, most of it indexing the regions.

**Function regions.** `src/pytest_coverset/regions.py` reproduces the control plane's rules (`coverset.analysis.functions.index_functions`) without depending on coverset: regions found through `ast.iter_child_nodes`, so definitions inside `if`, `try`, `with`, `for` and `match` count; `def_line` the first decorator line; `body_start` the first statement after a docstring (`def_line` for a docstring-only function); CPython's `co_qualname` with `<locals>`; `ordinal` among same-qualname definitions in source order; `key` = blake2b-8 of `path\0qualname\0ordinal`; the same AST-dump `body_hash` and `sig_hash`; kinds `function`, `method`, `class` and `module`. `tests/plugin_conformance/test_regions_match.py` pins the two implementations together field by field on every `.py` file of the plugin's fixture project and of `examples/demo-app/src`, and on edge cases (overloads, property setters, nested and conditional definitions, PEP 695 type parameters, a file that does not parse). The hashes are `ast.dump` digests, which differ between Python minor versions, so compare them only with hashes from the same interpreter version.

**Types.** `functions.jsonl` carries no `types`: for Python the control plane derives parameter type descriptors from the sources itself, and `coverset bundle convert PATH --to DIR --source-root SRC` adds them when it converts a Python bundle into neutral files.

```sh
pytest --coverset-out .coverset --coverset-neutral
coverset bundle validate .coverset/neutral
coverset report .coverset
```

## Running in parallel

There are four ways a suite runs on more than one core, and they compose: coverset shards across sandboxes, a CI matrix uploads several shards, pytest-xdist splits one pytest run across processes, and Vitest spreads test files across workers. Each ends in one bundle per shard; the control plane merges shards.

| Where | What splits the suite | Who merges | Notes |
|---|---|---|---|
| coverset on Modal (or the local executor) | the planner: `K = ceil(Σ p50 / target_shard_seconds)` shards, capped by `max_shards`, balanced longest-first ([docs/orchestrator.md](orchestrator.md)) | the control plane's ingest, one bundle per shard | the runner never passes `-n` (`PythonAdapter.test_command` adds `--numprocesses=0` when xdist is installed): one pytest process per sandbox, so per-shard wall time is what the planner predicts |
| your CI, upload mode | your matrix (`pytest --coverset` in each job, any split you like, `pytest-split`, `--splits`, ...) | the server: `coverset-ci upload --label <job>` per job, one shard per label, merged into one run per commit ([docs/upload.md](upload.md)); `--shards N` holds the verdict until N labels are in | each job may itself use `-n` (next row): the label's bundle is already merged |
| one machine, pytest-xdist | `pytest --coverset -n 8` (`--dist load`, the default, or `--dist loadfile`) | the xdist controller, at session end ([below](#pytest-xdist)) | local workers only |
| Vitest | Vitest's pool: test files across worker processes (`forks`) or threads | the reporter, from one spool file per worker | exact for any pool and for `--no-file-parallelism`; concurrent tests inside one file are attributed as a superset ([docs/typescript.md](typescript.md#parallelism)) |

### pytest-xdist

`pytest --coverset -n 8` (or `--coverset-out DIR -n auto`) gives one bundle in `DIR`, the same files a serial run writes. `pytest_coverset/parallel.py` has the two roles and `merge.py` the merge:

* **The controller**, the process you started, runs no test, so it measures nothing. Before xdist starts its workers it removes what an earlier run left in `DIR/workers/` and publishes `COVERSET_XDIST` (the output directory, whether the run is a local one, whether events are streamed, a run id) in the environment the workers inherit. It also puts each worker's directory in `workerinput["coverset_out"]`; the worker checks that the two agree. The environment variable is what starts the worker early enough: a worker's conftest files are imported before `workerinput` exists, and coverage must already run by then to measure their imports. The controller removes `DIR/workers/` only when coverset made it: it writes `DIR/workers/.coverset-xdist` (the run id) when it creates the directory, removes an existing one only when that marker is there, and reuses an empty one; any other `workers/` (a project's own `workers` package under `--coverset-out=.`, a CI artifacts directory) is a usage error (exit 4) and is never touched (`test_a_foreign_workers_directory_is_never_removed`). After a clean merge it removes the directory only if the marker still holds this run's id.
* **Each worker** (`gw0`, `gw1`, ...) pops `COVERSET_XDIST` at once (the tests' subprocesses never see it) and runs the whole plugin into `DIR/workers/<gw>/`: coverage with its own data file and one context per test, the shape tracer, `results.jsonl`, `collect.json`. It writes no tarball, no `coverage.json` and no neutral export, keeps coverage.py's own file names, and finishes its files inside `pytest_sessionfinish`, before xdist reports the worker done (`workeroutput["coverset"]` says what it wrote).
* **The merge** (`merge.py`), at the controller's session end: `coverage.sqlite` is the workers' data files combined with coverage.py's public `CoverageData.update` (which keeps every context), then loaded, reported to `coverage.json` and renamed to repository paths once, exactly as a serial run does; `results.jsonl` is every worker's rows; `collect.json` the first worker's collection (every worker collects the whole suite; xdist checks they agree); `shapes.jsonl.gz` re-indexed into one header, samples added, tests united, the example kept that a serial run would keep (the one from the earliest test in collection order), a function truncated when any worker capped it. Import time is counted once: every worker imports the same modules, so an observation whose caller is `<import>` keeps the largest count of any worker and a function's `calls` counts its import-time calls once. Then the usual `meta.json`, `bundle.tar.gz`, neutral export, events and `--coverset` summary line.
* **Events**: the controller emits `collected` (from xdist's collection), one `test` event per test, folded from the reports xdist forwards (workers attach the exception type of a failure to the report, so `fail_sig` matches the worker's own row; `test_failures_fold_the_same_on_the_controller`), `heartbeat`, `bundle_written`, `neutral_written` and `done`. Workers write their own events to `events.jsonl` in their directory only when the controller streams events.
* **`meta.json`** of the merged bundle: `tracer`, `tool_id`, `tracer_lost_at`, `tracer_losses`, `coverage_warnings`, `functions_traced`, `functions_truncated`, `tracer_errors` and the tracer timings come from the workers (summed or the first one set), every worker error is in `errors` prefixed `xdist worker <gw>:`, and `xdist` is `{"role": "controller", "run", "dist", "workers": [{"id", "tests", "exit_code", "session_wall_s", "errors", "finished"}], "nodes_down", "merge_s"}` (`null` for a run without workers). There is one `workers` entry per worker directory, named after it, so a worker that died is listed as itself with `finished: false`, `tests: 0` and `exit_code: null`.
* **When a worker dies** (a test calls `os._exit`, the OOM killer): xdist reports the running test as failed and starts a replacement worker. The merge keeps every other result, uses the controller's row for the dead test (`failed`, `fail_sig` of `xdist.WorkerCrashed`), keeps the coverage of every test the dead worker had finished (it committed each at its switch; only the dying test's own lines are lost), says `meta.json missing: the worker did not finish` in `errors`, and keeps `DIR/workers/` for a look; after a clean merge it is removed (`test_a_crashed_worker_keeps_the_rest_and_says_so`).

What is equal to a serial run: on the plugin's fixture project the merged bundle and a serial bundle have the same `(context, file, arc)` rows, results (all but timings), decoded shape rows (examples included), `coverage.json` (all but its timestamp), `collect.json`, invariant links and neutral coverage rows, with `-n 2` and `--dist load` and `--dist loadfile` (`test_the_merged_bundle_equals_the_serial_one`). What can differ on other suites, by design:

* **Module- and class-scoped fixtures under `--dist load`.** A module's tests can land on several workers; the fixture is set up once per worker, and its lines go to the test that set it up there. Use `--dist loadfile` (or `loadscope`) when fixtures are heavy; it also saves the repeated set-up.
* **Adaptive sampling.** Each worker samples a function until it settles, so with N workers a hot function can be sampled up to N times the per-session cap; `calls` is "sampled calls" either way, and a class missing from a capped function still reads "not observed in N sampled calls".
* **Test order.** `results.jsonl` is in collection order (a serial run's is execution order).

Only local workers can be merged (their files on this disk, their names relative to this working directory): `--tx ssh=...`, `--tx socket=...`, `popen//chdir=...` and `--rsyncdir` are a usage error (exit 4) before any worker starts. `-n 0`, `--collect-only` and `-p no:xdist` are serial runs. `--coverset-stop-file` is checked by every worker before each of its tests; the first to see it stops, xdist stops the run (`Interrupted: coverset: stop requested`, exit 2), and the tests that never ran are `not_run` in the merged bundle, with `stop_requested: true`. `--coverset-scores` works too: every worker sorts its collection the same way, and xdist hands tests out in that order.

Cost: the merge took 0.07 s for 1,200 tests on 4 workers and 0.10 s on 8 (`meta.xdist.merge_s`), plus the usual `coverage.json` and neutral export once on the merged data. Scaling numbers are under [Overhead](#overhead).

### A suite whose tests run pytest

coverset's own suite runs pytest inside its tests (pytester, the demo's `LocalExecutor`, `coverset-ci`, the report fixtures), starts the Quart app and spawns subprocesses, and `pytest --coverset -n 6` gives the same outcomes as the plain run ([docs/dogfood.md](dogfood.md)). What keeps the outer run and the inner ones apart:

* **The environment.** In an instrumented run `COVERSET`, `COVERSET_EVENT_NONCE` and `COVERSET_XDIST` are removed in the plugin's first hook, before any conftest file is imported, so no test and no subprocess sees them. A pytest started by a test on a worker inherits `PYTEST_XDIST_WORKER` but not the token, so it is an ordinary run: instrumented only with options of its own, into its own directory, with workers of its own if it asks for them.
* **In-process runs** (`pytest.main`, `pytester.runpytest_inprocess`) build a new configuration: the plugin's hooks run for it, find no `--coverset-*` option and stay inert. An instrumented in-process run cannot measure under an instrumented outer one (coverage's tracer is taken) and is a usage error.
* **The configuration.** The workers see the controller's rootdir and configuration file (value words glued, above), and the merge compares each worker's `rootdir` and `configfile` with the controller's: a difference is an error in `meta.errors` and the worker directories are kept (`test_workers_with_another_configuration_are_an_error`). A run whose workers had lost the project's `pyproject.toml` used to merge into a bundle with no error at all.

`test_tests_that_run_pytest_stay_out_of_the_run` runs, under `-n 2 --coverset-out=out`, tests that start pytest in subprocesses (plain, instrumented, with workers of their own) and in-process, and checks that the outer bundle holds exactly the outer tests and no error.

## Hypothesis

Hypothesis and coverset coexist on 3.12 and later. Hypothesis's scrutineer claims `sys.monitoring` tool id 3; coverset never asks for 3. A `@given(st.floats())` test is traced like any other, and the plugin's tests check that `nan` and an infinity are recorded for such a parameter (`test_recorded_shapes`).

Tracing makes every traced call slower, and Hypothesis's default deadline of 200 ms per example can then fail tests that pass without coverset. Register a profile without a deadline and load it in `conftest.py`, as the demo app does (`examples/demo-app/tests/conftest.py`):

```python
from hypothesis import settings

settings.register_profile("coverset", deadline=None, print_blob=True, derandomize=True)
settings.load_profile("coverset")
```

The plugin does not change Hypothesis settings itself. It records the active default deadline in `meta.json` (`hypothesis.deadline_ms`; `null` means no deadline), so a reader can tell which profile a shard ran under.

## Overhead

`packages/pytest-coverset/scripts/measure_overhead.py` measures what each layer costs. It runs a suite in up to five modes, interleaved so drift hits all of them, in a temporary copy, and prints the median wall time of each (a `python -m pytest` subprocess, start-up included), each mode's multiplier over plain pytest, and the median of the plugin's own timings from `meta.json`:

| Mode | What runs |
|---|---|
| `plain` | `pytest` |
| `covpy` | `coverage run --branch --source=...` with `COVERAGE_CORE=ctrace`: coverage.py alone, one context for the whole run |
| `coverage` | the plugin with `--coverset-shapes=off`: one context per test, results, the bundle |
| `coverage_shapes` | the plugin with the shape tracer (what the runner passes; `overhead_x`) |
| `neutral` | the plugin, the shape tracer and `--coverset-neutral` (what `--coverset` does) |

```sh
uv run python packages/pytest-coverset/scripts/measure_overhead.py                       # fixture project
uv run python packages/pytest-coverset/scripts/measure_overhead.py --target examples/demo-app --source ledger
uv run python packages/pytest-coverset/scripts/measure_overhead.py --target examples/bench-suite --source benchsuite \
    --conftest-append packages/pytest-coverset/scripts/stub_busy_conftest.py              # 1,200 near-empty tests
uv run python packages/pytest-coverset/scripts/measure_overhead.py ... --workers 8 --modes plain,neutral   # with xdist
```

### Layer by layer

Median of 5 interleaved runs, Python 3.13.0, coverage.py 7.16.2, an M4 Max (16 cores) shared with other work (load average about 6), 2026-10-13. "Before" is the plugin at the start of this change (`git archive HEAD`), measured in the same session. The stubbed bench suite is `examples/bench-suite` with `busy()` turned into a no-op (`stub_busy_conftest.py`): 1,200 tests that do almost nothing, so per-test costs dominate.

| Suite | plain | coverage.py alone | + per-test contexts | + shapes | + neutral export |
|---|---|---|---|---|---|
| demo app, 97 tests | 2.63 s | 1.13× | 1.13× (before: 1.15×) | 1.17× (1.17×) | 1.18× (1.23×) |
| bench suite, 1,200 stubbed tests | 0.99 s | 1.91× | 2.13× (before: 2.64×) | 2.14× (2.68×) | 2.12× (3.03×) |

Read across a row, the steps are what each layer adds. On the stubbed bench suite: coverage.py's C tracer alone costs 0.9 s for 1,200 tests (it runs on every call pytest itself makes, 0.75 ms per test); per-test contexts and the plugin's bookkeeping add 0.2 s (of which the context switches are 0.12 s, `meta.timings.context_switch_s`); the shape tracer adds 0.02 s here (its callbacks totalled 0.018 s, `tracer_callback_s`); the neutral export 0.065 s. Before this change the same three plugin layers added 0.5 s, 0.03 s and 0.3 s.

What was fixed, measured by itself (`cProfile` over a run, then each fix with the output compared byte for byte or as the tests compare it):

| Cost | Before | After | How |
|---|---|---|---|
| a context switch (coverage.py writes the previous test's data) | 177 µs | 25 µs (microbenchmark: 2,000 switches of a one-function module) | one SQLite connection held across writes, committed once a second; an xdist worker commits at every switch (about 51 µs) so a crash keeps its finished tests ([When data reaches disk](#when-data-reaches-disk)) |
| the neutral export, 1,200 stubbed tests | 0.35 s | 0.065 s | the same: coverage.py connected and closed around each of its 2,400 per-(test, file) queries |
| the neutral export of a real 5,081-test bundle (123 MB `coverage.sqlite`, 115,573 coverage rows) | 29.9 s | 9.8 s, identical files | the held connection (to 12.0 s), then one set union per file instead of 10 million set inserts when arcs supply the lines (to 9.8 s) |
| `bundle.tar.gz` of that bundle | 13.3 s (gzip level 9) | 3.6 s (level 6), 0.13% larger | `TARBALL_COMPRESSLEVEL` |
| `results.jsonl` | a `write` + `flush` per test | buffered, flushed every second and after a failure | `ResultsLog` |
| events when nobody listens (`--coverset`, no nonce) | a `dataclasses.asdict` copy and a JSON encoding per test | skipped | `EventWriter.enabled`; `TestResult.as_dict` builds the dict directly |
| test file paths | two `realpath` calls per test, twice | once per file | `CoversetSession._rel` |

The coverage.py core is now most of what is left, and it is per call, not per test: see [What would make it faster](#what-would-make-it-faster).

### Overhead on tight loops

A suite's multiplier hides how a single hot function behaves. Wall time relative to plain pytest, median of 3 repeats on this Mac, 2026-10-11. The tight-loop project calls a one-line `add(a, b)` 3 million times from one test (`--coverset-source tpkg`); "full-trace" means `add`'s file is on the full-trace list.

| Suite | Mode | 3.13 | 3.12 | Measured |
|---|---|---|---|---|
| plugin fixture project (24 tests) | coverage + shapes | 1.39× | 1.46× | review |
| demo app (97 tests) | coverage + shapes | 1.18× (one interpreter) | | review |
| tight loop | coverage only | 2.5× | 2.5× | review; re-measured 2.45× / 2.51× |
| tight loop | coverage + shapes, adaptive cap | 2.5× | 2.6× | review; re-measured 2.51× / 2.57× |
| tight loop | coverage + shapes, full-trace, no cap (before the safety cap) | 10.5× | 12.2× | review |
| tight loop | coverage + shapes, full-trace, 100,000-call safety cap | 2.8× | 2.9× | after the cap: 2.84× / 2.87× |

The re-measurements ran `measure_overhead.py --python <venv> --target <tight-loop copy> --source tpkg` (adding `-- --coverset-full-trace=full.txt` for the full-trace rows; medians 2.23 s and 2.24 s against 0.79 s and 0.78 s plain). With the cap, a hot changed function costs about what coverage alone costs.

### Scaling with workers

`examples/bench-suite` as generated (1,200 CPU-bound tests, 174 s of `busy()` in all), on one machine with pytest-xdist and the neutral export on, one run serial and the median of 3 with workers, same day and machine as above:

| Workers | plain | `--coverset-out ... --coverset-neutral` | merge (`meta.xdist.merge_s`) | `bundle.tar.gz` | `neutral.tar.gz` |
|---|---|---|---|---|---|
| none | 208.5 s | 181.0 s | | 342 KB | 45 KB |
| 4 | 50.4 s | 48.6 s | 0.08 s | 355 KB | 73 KB |
| 8 | 29.1 s | 25.8 s | 0.10 s | 365 KB | 90 KB |

The instrumented runs are not slower here because this suite cannot show tracing cost: `busy()` calibrates its loop at import under whatever tracer is active, so a test costs its nominal milliseconds either way, and the difference between columns is noise from a shared machine. What the table does show: the run scales with workers as plain pytest does, the merge is a tenth of a second, `coverage.sqlite` is the same size (909 KB: the same contexts and arcs), and the neutral archive grows with workers because each worker samples shapes on its own ([pytest-xdist](#pytest-xdist)). With `busy()` stubbed, so that per-test overhead dominates, 1,200 tests take 1.42 s plain and 2.31 s instrumented on 4 workers (1.62×), 1.89 s and 2.83 s on 8 (1.49×), against 2.14× serial: per-test costs are spread over the workers like the tests.

### A 5,000-test application suite

A private Flask application's suite, 5,426 tests in 470 files, Postgres and Redis on localhost (one database per xdist worker, which the suite's own conftest sets up), measured once each on 2026-10-13 on the same shared machine, outputs written outside the repository (`--coverset-out=<elsewhere> --coverset-source=flask --coverset-neutral`, `-p no:cacheprovider`, `PYTHONDONTWRITEBYTECODE=1`; `git status --ignored` was identical before and after):

| Run | Wall time | Notes |
|---|---|---|
| `pytest` | 549.7 s | |
| `pytest --coverset-out ... --coverset-neutral` | 508.4 s | within run-to-run noise of plain: the tests wait on Postgres, and the tracer's cost hides in that wait |
| `pytest -n 8` | 84.5 s | |
| `pytest -n 8 --coverset-out ... --coverset-neutral` | 145.1 s | 1.72× plain `-n 8`, 3.5× faster than the serial instrumented run |

Where the instrumented runs spend their time (`meta.json`):

| Step | serial | `-n 8` |
|---|---|---|
| per-worker session (tests plus tracing) | 482.8 s | 108.6 s on each worker (plain: 81.4 s) |
| context switches, 5,426 | 14.6 s (2.7 ms each: the SQLite inserts of each test's arcs, about 130 MB in all) | 18.5 s summed over the workers |
| shape tracer callbacks | 2.1 s | 10.4 s summed (each worker samples on its own) |
| merge (data files, `coverage.json`, shapes) | | 11.9 s, of which `coverage.json` 4.2 s and shapes 1.7 s |
| `coverage.json` | 5.0 s | (in the merge) |
| `bundle.tar.gz` | 3.2 s (56 MB) | 2.6 s (44 MB) |
| neutral export | 9.9 s (5.8 MB) | 9.6 s (9.8 MB) |

So under `-n 8` the tracing costs about a third more per worker (eight workers and Postgres compete for the cores, so the tracer's CPU no longer hides in I/O waits), and the controller adds a serial tail of about 25 s after the last test: the merge, the report, the archive and the export. The same suite's instrumented serial run took 486 s before this change with a 21 s neutral export and a level-9 archive (an earlier run of the previous plugin on the same suite, from its `meta.json`); measured offline on that bundle, the export now takes 9.8 s instead of 29.9 s and the archive 3.6 s instead of 13.3 s. 5 tests failed in both serial runs and passed in both `-n 8` runs, plain and instrumented alike: a property of the suite, not of the plugin.

### What would make it faster

The budget now, per test and per call, and what would cut each part:

| Part | Cost today | What would make it smaller |
|---|---|---|
| coverage.py's ctrace core | about 0.75 ms per test of a near-empty suite, and a C callback on every Python call and line of measured code (2.5× on a tight loop) | **sysmon instead of ctrace.** coverage.py's `sys.monitoring` core disables each line after its first hit, so measured code runs at full speed once warm. It is refused today for two reasons ([Why the ctrace core](#why-the-ctrace-core)): branch coverage needs Python 3.14's `BRANCH_LEFT`/`BRANCH_RIGHT` events, and it has no dynamic contexts. The plugin does not need coverage.py's dynamic contexts: it could flush and re-arm per test itself (`sys.monitoring.restart_events()`, then one callback per distinct line and branch a test runs), so on 3.14 the per-test cost becomes "lines the test touched" instead of "every line event". `restart_events()` is global, so the shape tracer's `DISABLE`d non-targets come back once per test and are disabled again at one callback each. |
| our own line tracer | (not built) | The same idea without coverage.py: a `sys.monitoring` tool with `LINE` and `BRANCH` events on target code objects only (the shape tracer already finds them), a bitmap per code object per test, `DISABLE` after the first hit. It removes coverage.py's per-call file-name check on non-measured code (pytest's own calls, which are most of the 0.75 ms) and the SQLite writes per test (the data would go straight into the neutral format). The cost is reimplementing arc semantics coverage.py gets right (exceptions, generators, `finally`), so it is the second step, after sysmon on 3.14. |
| per-test bookkeeping | about 0.17 ms per test on the stubbed bench suite, 0.1 ms of it the context switch (coverage.py's write, the warning capture, the tracer check), the rest the result row and the report folding | little left; the switch could skip `warnings.catch_warnings` (coverage.py does not warn on a switch in practice) |
| shapes | 2 s of callbacks over the 5,081-test suite below, capped per function | the caps already bound it; a hot changed function costs what coverage alone costs |
| finalisation | `coverage.json` 2.3 s, neutral export 9.8 s, tarball 3.6 s on the 5,081-test bundle | the export reads arcs once per (test, file) pair through coverage.py's public API and once more to find the pairs; one SQL pass over the `arc` table would halve it, at the price of depending on coverage.py's schema |
| the xdist merge tail | about 25 s after the last test on the 5,426-test suite (combining data files 6 s, `coverage.json` 4 s, shapes 2 s, archive 3 s, export 10 s) | most of it could run in the workers, in parallel: every context but `<import>` lives in exactly one worker, so each worker could write its own neutral coverage rows and the controller would only concatenate them and merge `<import>` |
| one process | the serial suite | `-n N` ([pytest-xdist](#pytest-xdist)), or shards (coverset on Modal, or upload mode with one label per CI job) |

## Running it by hand

From a repository whose tests import the code under `src/` (the demo app sets `pythonpath = ["src"]` in its pytest configuration), the local workflow is:

```sh
pytest --coverset
coverset report .coverset --open
```

The explicit form, what the runner's flags amount to:

```sh
python -m pytest --coverset-out out --coverset-source ledger
```

`out/` then holds the eight files and `bundle.tar.gz` (plus `neutral/` and `neutral.tar.gz` with `--coverset-neutral`). On a copy of `examples/demo-app` at the branch head this ran 97 tests, traced 34 functions, capped 4 of them, and recorded `tool_id` 4 and `tracer` `CTracer` in `meta.json`.

Neither form prints event lines: they are written only when `COVERSET_EVENT_NONCE` is set and `--coverset` is not, as in a shard, and then go to stdout among pytest's own output.

What a shard runs, in full, is closer to this:

```sh
COVERSET_EVENT_NONCE=<32 hex> PYTHONPATH=<repo>/src:<repo> \
python -m pytest @run/shard.args \
  --coverset-out=run/out --coverset-scores=run/shard.scores.json \
  --coverset-source=ledger --coverset-full-trace=run/full-trace.txt \
  --coverset-shapes=on --coverset-stop-file=run/STOP \
  --continue-on-collection-errors -p no:cacheprovider -q --rootdir=<repo>
```

The `=` in every value option is load-bearing ([why](#value-options-are-one-word---optvalue)).

`run/shard.args` holds one node id per line, verbatim, with no blank lines (a blank line would collect the whole rootdir). Duplicates collapse. `run/shard.scores.json` holds rank-derived scores (`len(shard) - index`) so the plugin's sort reproduces the planner's order (`analysis/sharding.py`, `shard_scores`).

The plugin's sort key is `(-max_score(module), module_path, -max_score(class), class_name, -score, nodeid)`. The riskiest module runs first and, within it, the riskiest class, so module- and class-scoped fixtures still set up once per group. Unknown node ids score 0. `[tool.coverset] reorder = false` makes the runner pass no scores file, and the shard then runs in the args-file order.

To test the plugin itself:

```sh
uv run pytest packages/pytest-coverset/tests -q -p no:cacheprovider
```

The check on Python 3.12, outside the workspace lock, is `uv run --isolated --no-project --python 3.12 --with-editable ./packages/pytest-coverset --with pytest==9.1.1 --with hypothesis==6.168.5 --with pytest-xdist==3.8.0 pytest packages/pytest-coverset/tests -q -p no:cacheprovider -n 8` (234 passed on 2026-10-13; without `--with pytest-xdist==3.8.0` and `-n 8`, 211 passed and `test_xdist.py` skipped). `--with-editable` matters: `--with ./packages/pytest-coverset` can reuse a cached wheel of an older build of the same version (`--no-cache` avoids that too, but needs the network).
