# pytest-coverset

The in-sandbox half of [coverset](../../README.md): a pytest plugin that records per-test coverage, the shapes of the arguments each function received, runs tests in the order the control plane asks for, and streams results as JSON lines. It also ships `coverset-ci wait`, a stdlib-only command for CI jobs that want to wait for a coverset verdict.

Python 3.12+ (the shape tracer uses `sys.monitoring`); dependencies: `coverage` and `pytest`.

## Quick start

```sh
pytest --coverset                  # run the suite instrumented; the bundle goes to <rootdir>/.coverset
coverset report .coverset --open   # build the pages and open them
```

`--coverset` (or `COVERSET=1` in the environment) is the local switch: the bundle goes to `<rootdir>/.coverset`, the packages to measure come from `[tool.coverset]` in `<rootdir>/pyproject.toml` (`source`, else the packages found under `source_root`, default `src` when it exists), the neutral bundle is written too, and one line at the end says where everything went:

```
coverset: wrote .coverset (24 tests, 4 files); `coverset report .coverset` for the pages
```

That line is the only thing the plugin prints: the JSON event lines ([Events](#events)) are for the orchestrator and are never written in a local run. When the sources measured no file at all (a misspelt `[tool.coverset] source = ["does_not_exist"]`), the line says so instead of a silent "0 files": `...; warning: no file of the measured sources (does_not_exist) was found: check [tool.coverset] source or --coverset-source` (also in `meta.json` `warnings`).

Any explicit `--coverset-*` option wins over these defaults. With nothing to measure (no `source`, no package found) the run stops with a usage error that says to set `[tool.coverset] source` or pass `--coverset-source`. A directory the plugin creates gets a `.gitignore` of `*`, so `.coverset/` is never committed by accident. An output directory that is a symlink is refused, and so is one reached through a symlinked directory inside the repository (`link/out`, `link/../out`), so a committed link cannot redirect the bundle; links above the repository root, or on the way to an output directory outside it, are not checked. The plugin removes `COVERSET` from the environment as soon as pytest starts, with or without `--coverset` on the command line, so pytest runs (and any other subprocess) your tests start never see it.

## Options

Installing the plugin changes nothing about a normal run except registering the `invariant(id)` marker. Instrumentation starts with `--coverset` or `--coverset-out`.

| Option | Default | Meaning |
|---|---|---|
| `--coverset` | off | instrument with the local defaults (quick start); `COVERSET=1` does the same |
| `--coverset-out DIR` | off (`<rootdir>/.coverset` with `--coverset`) | write the facts bundle to `DIR` (turns instrumentation on) |
| `--coverset-neutral` | off (on with `--coverset`) | also write the language-neutral bundle, `DIR/neutral/` and `DIR/neutral.tar.gz` (`COVERSET_NEUTRAL=1` does the same) |
| `--coverset-scores FILE` | off | JSON `{nodeid: score}`; sort by `(-max(module), module, -max(class), class, -score, nodeid)` |
| `--coverset-source PKG` | all in-repo code (`[tool.coverset]` with `--coverset`) | package name or path to measure and trace (repeatable) |
| `--coverset-trace-module MOD` | none | extra module (and submodules) to trace (repeatable) |
| `--coverset-full-trace FILE` | none | repo-relative paths, one per line, traced without the adaptive sample cap |
| `--coverset-shapes on\|off` | `on` | record argument and return shapes |
| `--coverset-repr-examples off\|scalars\|all` | `scalars` | redacted example values to keep |
| `--coverset-stop-file PATH` | none | once `PATH` exists, skip the remaining tests, write the bundle, exit 2 |
| `--coverset-shape-samples N` | 2048 | sampled calls per function before sampling stops |
| `--coverset-shape-settle N` | 64 | consecutive calls with no new shape before sampling stops |
| `--coverset-full-trace-cap N` | 100000 | safety cap on sampled calls per full-trace function (recorded as `truncated`, `cap: "full"`) |

Every option takes `--opt value` or `--opt=value`. Scripts that pass paths outside the repository should use the `=` form: pytest looks for its ini file before plugins register their options, so a separate path word can move the rootdir and drop the repo's pytest configuration. `--coverset-out` with pytest-xdist workers (`-n 2`, `-n auto`) is a usage error, and so are Python below 3.12 and an output directory that is a symlink, lies below a symlink inside the repository, or is a file.

Coverage uses the ctrace core with one dynamic context per test whose name is exactly the nodeid (no static context is prefixed, so `|` in parametrize ids needs no special parsing); the `''` context holds imports and collection. A foreign `sys.settrace` function at start is a usage error; a tracer lost later (a test calling `sys.settrace(None)`, or pytest-cov started without `--no-cov`) is recorded as `tracer_lost_at`/`tracer_losses` in `meta.json` and re-installed when possible. coverage.py's own warnings (for example a `--coverset-source` package the shard never imports) are captured into `meta.json` `coverage_warnings` instead of raised, so target repos that run with `-W error` or `filterwarnings = error` are unaffected, and an error while recording a result or emitting an event goes to `meta.json` `errors` instead of aborting the run. A conftest that fails to import still yields a bundle and a `done` event with exit code 4.

## Stopping

Two graceful stops, both ending with exit code 2, a complete bundle and the `bundle_written` and `done` events; tests that did not finish count as `not_run`:

- create the `--coverset-stop-file` (checked before each test, so a test that is running finishes first);
- send SIGTERM. The plugin turns the first SIGTERM into a `KeyboardInterrupt`, which interrupts even a hung test, and ignores later ones while the bundle is written. Wait a few seconds (the runner's 5 s) before escalating to SIGKILL, which loses the in-memory shapes and the bundle.

## Shapes

Shape keys follow plan Addendum B. Infinities do not count as the finite sign classes: `+inf` is `float|inf` and `-inf` is `float|inf|inf-neg`. Varargs parameters are labelled with their real names (`*args`, `*a`, `**kw`), so consumers should treat any label that starts with `*` as varargs. Example values are redacted when the name they are bound to looks secret (`pass|secret|token|key|auth|cookie|session`): the parameter name, the function name for `return` values (`make_reset_token`), and `str` dict keys inside container examples. Ints wider than 128 bits print as `<int N bits>`.

## Bundle

`DIR/bundle.tar.gz` holds `coverage.sqlite`, `coverage.json` (format 3), `shapes.jsonl.gz` (plan Addendum B), `results.jsonl` (one final row per test, authoritative), `collect.json` (`items` with file, line, markers, invariant ids, hypothesis flag; `errors` for collection errors), `durations.json`, `invariant_links.jsonl` and `meta.json` (versions, `tracer`, `tracer_lost_at`, `tool_id`, `git_root`, `repo_root`, `rootdir`, `cwd`, `coverage_paths`, `configfile`, `shapes_disabled_reason`, `tracer_errors`, counts, `errors`, `warnings`, timings).

Every path is repository-relative (POSIX, relative to the git root, or the rootdir when there is no `.git`), whichever directory pytest runs in: coverage.py names files relative to the working directory, and the plugin rewrites `coverage.json` and `coverage.sqlite` to repository paths when the run ends (`meta.coverage_paths: "repository"`). `meta.cwd` and `meta.rootdir` say where the run happened, relative to the same root. Test node ids stay pytest's (rootdir-relative). Should that rewrite fail (`meta.coverage_paths: "cwd"`, the reason in `meta.errors`), readers join `meta.cwd` in front of coverage.py's relative names; but from a directory that does not contain the sources (pytest run from `tests/`, code in `src/`) those names are absolute, every reader drops them, and that run's coverage is lost while the rest of the bundle reads normally.

## Neutral bundle

With `--coverset-neutral` (or `COVERSET_NEUTRAL=1`) the plugin also writes the language-neutral bundle of coverset's `docs/neutral-bundle.md` into `DIR/neutral/` and packs it, flat, as `DIR/neutral.tar.gz` next to `bundle.tar.gz`: `manifest.json` (`meta` is the same mapping as `meta.json`; `shard` comes from `COVERSET_SHARD`), `tests.jsonl.gz` (every collected test, `outcome` null for one that never ran), `files.jsonl.gz`, `coverage.jsonl.gz` (per test and file, statement first lines, `<import>` for code outside tests, with the test's arcs), `arcs.jsonl.gz`, `functions.jsonl.gz` (function regions by the control plane's rules, `regions.py`; no `types`, which the control plane derives from the sources for Python), `shapes.jsonl.gz` (self-contained `obs` and `sampling` rows) and `links.jsonl`. It is built from the files already written, after measurement stopped, so coverage, shapes and results are identical with and without the flag; it is written on every path that writes `bundle.tar.gz`, after `meta.json` and `bundle.tar.gz` (an interrupt during the export never costs the Python bundle). `meta.json` is then rewritten with the outcome in `neutral` (`status` `written`, `failed` or `interrupted`, `rows`, `dropped`, `seconds`); the `neutral_written` event (`path`, `sha256`, `bytes`, `seconds`, `rows`, `dropped`) precedes `done` and is emitted only when every file, the manifest and the archive were written. Sources are decoded as CPython decodes them (BOM, PEP 263 cookie), lines are counted as Python counts them, test ids over 512 characters are left out and counted, and stale files of an earlier export (either `.jsonl` variant, an old `neutral.tar.gz`, also when the flag is off) are removed. The export takes about 0.013 s on the fixture project.

```sh
pytest --coverset-out .coverset --coverset-source mypkg --coverset-neutral   # the explicit form
coverset report .coverset --open
```

## Events

Events are written only when `COVERSET_EVENT_NONCE` is set (the orchestrator sets a fresh one per exec) and the run is not a local one (`--coverset`/`COVERSET=1`); otherwise nothing is printed. Each event is one stdout line: `"\x1ecoverset <COVERSET_EVENT_NONCE> " + JSON`, kinds `collected`, `test`, `heartbeat`, `bundle_written`, `neutral_written` (only with `--coverset-neutral`), `done`. The JSON is ASCII-only, so any stdout encoding works. Split stdout on `\n` only: Python's `str.splitlines()` treats `\x1e` as a line break. The plugin removes `COVERSET_EVENT_NONCE` from the environment before conftest files load, so tests and their subprocesses do not inherit it; the nonce separates events from log text and is not a security boundary, so reconcile streamed rows against `results.jsonl`.

## Args files

`python -m pytest @shard.args` reads one nodeid per line, verbatim, and collects exactly those items in that order (duplicates collapse). Write `\n`-joined nodeids with no blank lines (a blank line collects the whole rootdir) and no trailing whitespace; non-ASCII ids are written as pytest prints them (`\xe9`).

## coverset-ci

```
coverset-ci wait --server URL --repo-id N --sha SHA [--timeout 1800] [--poll-interval 5] [--junit out.xml] [--token-env NAME]
```

Exit codes: 0 success, 1 failure, 2 interrupted, cancelled, timed out or neutral, 3 network/server/auth error after retries, 5 no tests.

## Overhead

`uv run python packages/pytest-coverset/scripts/measure_overhead.py` prints median wall times for plain pytest, coverage only, and coverage plus shapes.
