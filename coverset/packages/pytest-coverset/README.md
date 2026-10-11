# pytest-coverset

The in-sandbox half of [coverset](../../README.md): a pytest plugin that records per-test coverage, the shapes of the arguments each function received, runs tests in the order the control plane asks for, and streams results as JSON lines. It also ships `coverset-ci wait`, a stdlib-only command for CI jobs that want to wait for a coverset verdict.

Python 3.12+ (the shape tracer uses `sys.monitoring`); dependencies: `coverage` and `pytest`.

## Options

Installing the plugin changes nothing about a normal run except registering the `invariant(id)` marker. Instrumentation starts with `--coverset-out`.

| Option | Default | Meaning |
|---|---|---|
| `--coverset-out DIR` | off | write the facts bundle to `DIR` (turns instrumentation on) |
| `--coverset-neutral` | off | also write the language-neutral bundle, `DIR/neutral/` and `DIR/neutral.tar.gz` (`COVERSET_NEUTRAL=1` does the same) |
| `--coverset-scores FILE` | off | JSON `{nodeid: score}`; sort by `(-max(module), module, -max(class), class, -score, nodeid)` |
| `--coverset-source PKG` | all in-repo code | package name or path to measure and trace (repeatable) |
| `--coverset-trace-module MOD` | none | extra module (and submodules) to trace (repeatable) |
| `--coverset-full-trace FILE` | none | repo-relative paths, one per line, traced without the adaptive sample cap |
| `--coverset-shapes on\|off` | `on` | record argument and return shapes |
| `--coverset-repr-examples off\|scalars\|all` | `scalars` | redacted example values to keep |
| `--coverset-stop-file PATH` | none | once `PATH` exists, skip the remaining tests, write the bundle, exit 2 |
| `--coverset-shape-samples N` | 2048 | sampled calls per function before sampling stops |
| `--coverset-shape-settle N` | 64 | consecutive calls with no new shape before sampling stops |
| `--coverset-full-trace-cap N` | 100000 | safety cap on sampled calls per full-trace function (recorded as `truncated`, `cap: "full"`) |

Every option takes `--opt value` or `--opt=value`. Scripts that pass paths outside the repository should use the `=` form: pytest looks for its ini file before plugins register their options, so a separate path word can move the rootdir and drop the repo's pytest configuration. `--coverset-out` with pytest-xdist workers (`-n 2`, `-n auto`) is a usage error, and so is Python below 3.12.

Coverage uses the ctrace core with one dynamic context per test whose name is exactly the nodeid (no static context is prefixed, so `|` in parametrize ids needs no special parsing); the `''` context holds imports and collection. A foreign `sys.settrace` function at start is a usage error; a tracer lost later (a test calling `sys.settrace(None)`, or pytest-cov started without `--no-cov`) is recorded as `tracer_lost_at`/`tracer_losses` in `meta.json` and re-installed when possible. coverage.py's own warnings (for example a `--coverset-source` package the shard never imports) are captured into `meta.json` `coverage_warnings` instead of raised, so target repos that run with `-W error` or `filterwarnings = error` are unaffected, and an error while recording a result or emitting an event goes to `meta.json` `errors` instead of aborting the run. A conftest that fails to import still yields a bundle and a `done` event with exit code 4.

## Stopping

Two graceful stops, both ending with exit code 2, a complete bundle and the `bundle_written` and `done` events; tests that did not finish count as `not_run`:

- create the `--coverset-stop-file` (checked before each test, so a test that is running finishes first);
- send SIGTERM. The plugin turns the first SIGTERM into a `KeyboardInterrupt`, which interrupts even a hung test, and ignores later ones while the bundle is written. Wait a few seconds (the runner's 5 s) before escalating to SIGKILL, which loses the in-memory shapes and the bundle.

## Shapes

Shape keys follow plan Addendum B. Infinities do not count as the finite sign classes: `+inf` is `float|inf` and `-inf` is `float|inf|inf-neg`. Varargs parameters are labelled with their real names (`*args`, `*a`, `**kw`), so consumers should treat any label that starts with `*` as varargs. Example values are redacted when the name they are bound to looks secret (`pass|secret|token|key|auth|cookie|session`): the parameter name, the function name for `return` values (`make_reset_token`), and `str` dict keys inside container examples. Ints wider than 128 bits print as `<int N bits>`.

## Bundle

`DIR/bundle.tar.gz` holds `coverage.sqlite`, `coverage.json` (format 3), `shapes.jsonl.gz` (plan Addendum B), `results.jsonl` (one final row per test, authoritative), `collect.json` (`items` with file, line, markers, invariant ids, hypothesis flag; `errors` for collection errors), `durations.json`, `invariant_links.jsonl` and `meta.json` (versions, `tracer`, `tracer_lost_at`, `tool_id`, `git_root`, `repo_root`, `rootdir`, `configfile`, `cwd`, `shapes_disabled_reason`, `tracer_errors`, counts, timings). Shape and result paths are relative to the git root (the rootdir when there is no `.git`); `coverage.json` and `coverage.sqlite` paths are relative to the working directory, which is the repo root in a worker.

## Neutral bundle

With `--coverset-neutral` (or `COVERSET_NEUTRAL=1`) the plugin also writes the language-neutral bundle of coverset's `docs/neutral-bundle.md` into `DIR/neutral/` and packs it, flat, as `DIR/neutral.tar.gz` next to `bundle.tar.gz`: `manifest.json` (`meta` is the same mapping as `meta.json`; `shard` comes from `COVERSET_SHARD`), `tests.jsonl.gz` (every collected test, `outcome` null for one that never ran), `files.jsonl.gz`, `coverage.jsonl.gz` (per test and file, statement first lines, `<import>` for code outside tests, with the test's arcs), `arcs.jsonl.gz`, `functions.jsonl.gz` (function regions by the control plane's rules, `regions.py`; no `types`, which the control plane derives from the sources for Python), `shapes.jsonl.gz` (self-contained `obs` and `sampling` rows) and `links.jsonl`. It is built from the files already written, after measurement stopped, so coverage, shapes and results are identical with and without the flag; it is written on every path that writes `bundle.tar.gz`, and the `neutral_written` event (`path`, `sha256`, `bytes`, `seconds`, `rows`, `dropped`) precedes `done`. On the fixture project it adds about 0.03 s.

```sh
pytest --coverset-out .coverset --coverset-neutral
coverset report .coverset
```

## Events

Each event is one stdout line: `"\x1ecoverset <COVERSET_EVENT_NONCE> " + JSON`, kinds `collected`, `test`, `heartbeat`, `bundle_written`, `neutral_written` (only with `--coverset-neutral`), `done`. The JSON is ASCII-only, so any stdout encoding works. Split stdout on `\n` only: Python's `str.splitlines()` treats `\x1e` as a line break. The plugin removes `COVERSET_EVENT_NONCE` from the environment before conftest files load, so tests and their subprocesses do not inherit it; the nonce separates events from log text and is not a security boundary, so reconcile streamed rows against `results.jsonl`.

## Args files

`python -m pytest @shard.args` reads one nodeid per line, verbatim, and collects exactly those items in that order (duplicates collapse). Write `\n`-joined nodeids with no blank lines (a blank line collects the whole rootdir) and no trailing whitespace; non-ASCII ids are written as pytest prints them (`\xe9`).

## coverset-ci

```
coverset-ci wait --server URL --repo-id N --sha SHA [--timeout 1800] [--poll-interval 5] [--junit out.xml] [--token-env NAME]
```

Exit codes: 0 success, 1 failure, 2 interrupted, cancelled, timed out or neutral, 3 network/server/auth error after retries, 5 no tests.

## Overhead

`uv run python packages/pytest-coverset/scripts/measure_overhead.py` prints median wall times for plain pytest, coverage only, and coverage plus shapes.
