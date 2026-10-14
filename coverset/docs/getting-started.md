# Getting started: putting a repository on coverset

Start with `coverset doctor`. It makes the decisions coverset will make about a repository, says how it
made each one, tries the ones that can only be checked by doing them (building the environment,
importing the packages, collecting the tests), and prints the fixes for anything that will go wrong.
It never runs a test and never writes to the repository.

```sh
coverset doctor --repo ~/src/myrepo                 # the full report, collection included
coverset doctor --repo ~/src/myrepo --no-collect    # seconds: no environment build, no collection
coverset doctor --repo ~/src/myrepo --json          # the same report as JSON
coverset doctor --repo ~/src/myrepo --write-config  # also append the suggested [tool.coverset] to
                                                    # <data dir>/doctor/myrepo.toml (--config-out PATH)
```

`--write-config` never writes inside the repository: when the data dir would be inside it (no
settings, run from the repository's root, so `./data`), the default becomes
`~/.cache/coverset/doctor/<repo>.toml`, and a `--config-out` inside the repository is refused before
the diagnosis starts.

It exits 0 when the verdict is `ready` or `ready, with warnings` and 1 when any finding is an error.
Each finding has a fix; the suggested `[tool.coverset]` section at the end holds every value the
doctor inferred, with a comment saying why, and parses as valid configuration.

Both ways in below need a control plane that GitHub and your CI can reach. For a team, host it
with its state in **Postgres and an S3-compatible bucket** (`COVERSET_DATABASE_URL`,
`COVERSET_BLOB_URL`): the container is then stateless, so Fly.io, ECS Fargate, Modal, Railway,
Render or one VM with compose all work, and `scripts/smoke_hosted.py` checks a new database and
bucket end to end. For a trial, the laptop shape (the local Postgres and files) behind an ngrok
tunnel is enough (`coverset dev --tunnel`). [docs/deploy.md](deploy.md) has the requirements, the
recipes and the costs; [docs/dev.md](dev.md#hosted-like-development) the same Postgres + MinIO shape
on your machine.

## Two ways in

| | A. Upload mode | B. Managed runs |
|---|---|---|
| Where the tests run | your existing CI job | coverset's workers: Modal sandboxes, or local subprocesses |
| When they start | when your CI runs them | when the pull request webhook arrives, before any CI runner boots |
| Ordering, fail-fast, sharding | no (your CI decides) | yes: likeliest failures first, wave 1, shards dealt by duration |
| Environment | whatever your CI installs | built by coverset from the repository (see the source matrix below) |
| What the repository needs | `pytest-coverset` installed in the test job | a describable environment, importable packages, pytest config coverset can run with |
| Credentials | an upload token or the job's OIDC token | a GitHub App (or token + webhook), plus Modal for sandboxes |
| Setup | a hosted coverset ([docs/deploy.md](deploy.md)) and three lines of CI ([docs/upload.md](upload.md)) | a hosted coverset, the App, `COVERSET_EXECUTOR=modal`, `coverset warm` |
| Choose it when | the environment is exotic (system packages, services, GPUs), or you want results today | the suite is Python, its dependencies install from an index, and time-to-first-failure matters |

**A, upload mode.** Keep running the tests where they run:

```sh
pip install pytest-coverset
pytest --coverset              # writes the bundle to .coverset/
coverset-ci upload --server https://coverset.example.com --with-sources
```

coverset ingests the bundle and posts the same comment, check run and pages. Nothing in the rest of
this page is needed; [docs/upload.md](upload.md) has the workflow and the trust model.

**B, managed runs.** coverset builds the environment, puts the head commit into workers and runs the
tests itself. The repository needs:

1. **An environment coverset can describe**: a lock, requirement files or static metadata (the
   matrix below). `coverset doctor` names the source it picked and the first requirement lines.
2. **Importable packages.** coverset puts `source_root` on `PYTHONPATH` and measures the packages in
   `source`. Projects that only work installed (entry points, a version read from metadata at
   import, generated files, compiled extensions) say `install = "wheel"`, or let `auto` find out.
3. **A pytest configuration coverset can run with.** coverset adds its own options (one shard's node
   ids, `-p no:cacheprovider`, `--numprocesses=0` when pytest-xdist is installed, `--no-cov` when
   pytest-cov is); `addopts` that need the cache plugin or a missing plugin stop pytest with a usage
   error. The doctor lists them and the override that removes them.

Then, on the control plane: coverset hosted where GitHub can reach it ([docs/deploy.md](deploy.md)),
the GitHub App from `/setup` ([docs/github.md](github.md)), `COVERSET_EXECUTOR=modal` after `modal
setup` ([docs/modal.md](modal.md)), and `coverset warm --repo OWNER/NAME`, which builds the
environment image and the first trusted snapshot (where the project and the repository's own
packages get installed) before the first pull request.

## `[tool.coverset]`

Every key is optional. Inferred values are what `coverset doctor` prints, with how each was found.
A repository without a `pyproject.toml` (`setup.py`, `setup.cfg` or requirement files only) can add
one that holds nothing but `[tool.coverset]`: without a `[project]` table it does not change where
the environment comes from.
Scoped settings ([docs/settings.md](settings.md)) outrank `fail_fast`, `max_shards` and
`target_shard_seconds`, because the file comes from the pull request's head.

| Key | Default | What it does |
|---|---|---|
| `source` | inferred | Packages (or modules) to measure and trace, as dotted names: `["ledger"]`. Inferred from packages the build configuration names (`[tool.setuptools] packages`, hatch's wheel `packages`, Poetry's `packages`, `setup.cfg`/`setup.py`), else directories with `__init__.py` under `source_root` (plus top-level modules in a `src` layout), else PEP 420 namespace packages, else the module named like the project. |
| `source_root` | inferred | The directory put on `PYTHONPATH` (the repository root goes after it): `"src"`. Inferred from the build configuration's package directory (`package-dir`, `packages.find.where`, hatch, Poetry `from`), pytest's `pythonpath`, a `src/` holding code, the directory above the package that holds the tests (`flask/app/tests` gives `flask`), type-checker roots (`[tool.ty.environment] root`, pyright `extraPaths`, mypy `mypy_path`), else `.`. |
| `testpaths` | inferred | Where pytest collects: `["tests"]`. Else `testpaths` from pytest's own configuration file (entries that do not exist are left out, as pytest does; globs are expanded), else whichever of `tests/`, `test/`, `testing/` exist, else nothing (pytest collects from the root). |
| `python` | inferred | The Python version, 3.12 or later: `"3.13"`. Else `.python-version`, else the first of 3.13, 3.12, 3.14 that `requires-python` admits, else 3.13. |
| `requirements` | auto | Requirement files that define the environment, overriding every lock: `["requirements/ci.txt"]`. |
| `install` | `"auto"` | How the project itself is installed: `"none"` (imported from `PYTHONPATH` only), `"wheel"` (built and installed at snapshot time; the source still wins), `"editable"`, or `"auto"` (see [Installing the project](#installing-the-project)). |
| `pytest_args` | `[]` | Extra pytest options for collection and every shard: `["-p", "no:randomly", "-m", "not slow"]`, appended after the test paths at collection and last in every shard's command (before `--rootdir`), exactly as `coverset doctor` shows them. Options only. The options known to conflict with how coverset runs pytest are refused: writing outside the worker or moving pytest's root (`--basetemp`, `--rootdir`, `-c`, `--junitxml`, `--log-file`, `--report-log`), choosing tests by name or path or stopping a shard early (`-k`, `--deselect`, `--ignore*`, `--pyargs`, `-x`, `--maxfail`, `--lf`, `--sw`), coverset's own (`-n`, `--cov*`, `--coverset-*`, `-q`/`-v`, which change the collect listing), `-p` other than `-p no:NAME`, and `-o` keys that write files or change collection (`log_file`, `cache_dir`, `python_files`/`_classes`/`_functions`, `testpaths`, `norecursedirs`, `pythonpath`, `verbosity*`); an `-o addopts=...` value is checked word by word by the same rules. `-m` is allowed: a marker expression filters the suite identically at collection and in every shard. The list stops mistakes; it is not a sandbox (the repository's `conftest.py` runs in the worker anyway). |
| `invariants` | `"invariants.yaml"` | The invariants file ([docs/invariants.md](invariants.md)). |
| `trace_modules` | `[]` | Extra modules whose calls are traced for argument shapes. |
| `tracked_globs` | inferred | Paths whose change marks every test impacted: `uv.lock`, `conftest.py`, plus the environment's input files (`poetry.lock`, `Pipfile.lock` or the requirement files) when there is no `uv.lock`. |
| `max_shards` | `16` | Upper bound on shards per run (also capped by `COVERSET_MAX_SHARDS`). |
| `target_shard_seconds` | `20` | Shard size target: `K = ceil(sum of test p50s / target_shard_seconds)`. |
| `fail_fast` | `"finish_in_background"` | After the first confirmed failure: keep running the rest (`finish_in_background`) or stop (`cancel_remaining`). |
| `reorder` | `true` | Run tests in priority order (false: collection order, still sharded). |

## Where the environment comes from

The first source that applies wins. Every source ends as a pinned requirements file plus the
pytest-coverset wheel; `env_hash` covers the result, the Python version and the wheel, and the
`envs` row, the venv (LocalExecutor) or the image (Modal) is keyed by it.

| `env_source` | Detected by | Pinned by | Network | Notes |
|---|---|---|---|---|
| `requirements` (explicit) | `[tool.coverset] requirements` | `uv pip compile` | first resolution of new inputs | wins over every lock |
| `uv.lock` | `uv.lock` | the lock (`uv export --frozen --offline --no-hashes --no-emit-project --no-emit-workspace --no-emit-local --all-groups`, plus `--all-extras` when the project declares extras) | no | unchanged from before; path and workspace packages become local packages |
| `poetry` | `poetry.lock` | the lock, converted by coverset | no | lock formats 1.x and 2.x; markers from the lock (2.1) or from the dependency edges (older); every group and extra; `develop` path packages are editable local packages; a package from a named private source is pinned by name (set `UV_EXTRA_INDEX_URL` on the control plane) |
| `pipenv` | `Pipfile.lock` | the lock, converted | no | `default` and `develop`; `path`/`file` entries are local packages |
| `requirements` (auto) | `*requirements*.txt` at the top, `requirements/*.txt`, `reqs/*.txt` whose name says runtime (`requirements`, `base`, `main`, `prod`...) or test/dev (`test`, `tests`, `dev`...) | `uv pip compile` | first resolution | names with `min`, `docs`, `lock` are skipped (a `tests-min.txt` pins the oldest versions and conflicts with `tests.txt`); `-r`/`-c` includes are followed inside the repository; the project's own declared dependencies are added |
| `pyproject` | a `[project]` table, no lock | `uv pip compile` | first resolution | dependencies, every extra, the `test`/`tests`/`testing`/`dev` dependency groups (`include-group` followed) and uv's legacy `dev-dependencies`; `dynamic` dependencies read from `[tool.setuptools.dynamic]` files |
| `setup` | `setup.cfg` and/or `setup.py`, nothing else | `uv pip compile` | first resolution | `setup.cfg` `[options]` (`install_requires`, `extras_require`, `tests_require`); `setup.py` keyword arguments that are literals, or names bound to literals, read with `ast` and never executed. A `setup.py` that computes its dependencies needs `[tool.coverset] requirements` |

Not supported directly: `pdm.lock` (a PDM project has `[project]`, so it resolves as `pyproject`),
conda `environment.yml`, hatch environments (also `pyproject`), pip-tools `.in` files (the `.txt` beside
them is used), and dependencies that ship only an sdist (below).

**The resolver.** `uv pip compile --universal --no-build --no-header --no-annotate --python-version X`,
offline first against coverset's own uv cache (`~/.cache/coverset/uv-cache`), then online. The result
is cached under `~/.cache/coverset/requirements/<sha256 of the normalised inputs>.txt`, so the same
inputs always give the same pins, and after the first resolution no network is needed (`coverset
doctor --offline` proves it). `--universal` makes one resolution with markers for the macOS control
plane and Linux sandboxes alike. The pinned text starts with a header naming the source, the inputs
and the digest of what was resolved; a comment edited in `requirements.txt` changes nothing.

**What never happens on the control plane.** Environments are described for untrusted pull requests
too, on the machine that holds the forge credentials, so repository code never runs there: no
`setup.py` is executed, the project is never handed to the resolver, and `--no-build` makes a
dependency that ships only an sdist fail to resolve instead of running its build. Index options in
requirement files (`--index-url`, `--extra-index-url`, `--find-links`, `--trusted-host`) are dropped
with a note; private indexes are the operator's choice (`UV_INDEX_URL`, `UV_EXTRA_INDEX_URL`,
`UV_DEFAULT_INDEX`, `UV_INDEX` in the control plane's environment).

Direct references never reach the resolver either, because `uv pip compile` would fetch them on the
control plane, before anyone approved the pull request that wrote them. `name @ https://...`,
`name @ git+https://...` and bare VCS/URL lines (from requirement files, `[project]` or `setup`
metadata, `[build-system] requires`, a lock) are split out: only requirements by name are compiled,
and the references are appended verbatim after the pins under
`# coverset: direct references (not resolved by coverset)`, replacing any compiled pin of the same
package. Their own dependencies are resolved when a trusted build installs the environment; an
untrusted pull request whose environment holds one is never built, approved or not
([docs/orchestrator.md](orchestrator.md)). A direct reference used as a constraint is dropped. `file:`
URLs (`git+file://` included), absolute or relative filesystem paths given as a URL, and schemes other
than `http`, `https`, `ssh` and `git` are refused everywhere, locks included, with a note: they would
make the builder read a directory outside the checkout. Packages that live in the repository are
written as relative paths and become local packages (below).

## Installing the project

By default nothing is installed: the tests import the checkout through `PYTHONPATH`, which is what makes
a pull request's changes visible without a build. `install = "auto"` (the default) switches to
`"wheel"` when the repository has a build backend and one of these is true:

| Signal | Why it needs an installed copy |
|---|---|
| entry points the project reads itself: a declared group (`[project.entry-points]`, Poetry plugins, `setup.cfg`/`setup.py` `entry_points`) whose name appears in a source file that calls `entry_points`, or `pytest11` | `importlib.metadata.entry_points()` only sees installed distributions. Groups other tools read (`nbconvert.exporters`) do not count: the project's own tests do not discover them |
| a source package's `__init__.py` uses `importlib.metadata`, `pkg_resources` or `version(` | the version (or anything else) is read from installed metadata at import time |
| a version file generated by setuptools-scm (`version_file`/`write_to`) or hatch-vcs (`version-file`) that is not in git | the file exists only after a build |
| compiled extensions (`ext_modules`, `Extension(`, `cythonize(`, `[tool.setuptools] ext-modules`, or a maturin, scikit-build-core, meson-python or setuptools-rust backend) | the shared objects exist only after a build |

Console scripts alone do not switch it on (they are not put on `PATH` either way); package data does
not need it (it is read from the source tree). The doctor's import probe tells you when `auto` missed
something: an `import <package>` that fails from `PYTHONPATH` comes with the fix `install = "wheel"`.

What `wheel` does, by executor:

* **Modal**: when the trusted snapshot of a default-branch commit is built, the sandbox runs
  `pip install --no-deps --no-build-isolation --no-index /work/repo` (no network: the backend's
  `[build-system] requires` are added to the environment for exactly this) before collecting, so the
  snapshot holds the installed copy. `editable` runs `pip install -e` instead, which follows the
  checkout each pull request puts in `/work/repo` but supplies no compiled or generated files. A
  failed install fails the snapshot; runs keep the previous snapshot or the plain image. Runs that
  start from the env image (before the first snapshot) have no installed copy: run `coverset warm`.
* **LocalExecutor**: per (environment, commit), `uv pip install --no-deps --no-build-isolation --target
  ~/.cache/coverset/installs/<key>/site <checkout>`, never into the shared venv. The run's workers get
  that directory as a site directory (`COVERSET_SITE_DIRS`, read by a one-line `.pth` in the venv), so
  local runs always test the head's own build. `editable` is installed the same way (a worker's
  checkout is temporary).

Either way `PYTHONPATH` comes first, so every pure-Python module is imported from the checkout. The
installed copy supplies distribution metadata (entry points, `version()`), and a small import hook
(`_coverset_fallback.pth`, written next to the install) supplies what the source tree cannot: compiled
extension modules (matched by suffix) and the modules the build generated (computed at install time by
comparing the installed package with the source). A module a pull request deletes is not served from
the installed copy. On Modal, a pull request that changes C or Rust sources runs against the default
branch's build until the next snapshot; the doctor warns about it.

**Packages that live in the repository** (committed wheels, path dependencies, uv workspace members,
Poetry `develop` packages, `-e ./libs/x` in requirement files) cannot come from an index. They are
recorded in the requirements as `# coverset-local: <file|dir|editable> <path> <name> [; marker]` comment
lines (so they are part of `env_hash`) and installed with `--no-deps` in the same step, before the
project; a platform marker picks the right wheel. Files stored with Git LFS are the exception:
coverset exports commits with `git archive` from a bare mirror, which yields LFS pointers. Those are
skipped with a note, and the doctor reports them as an error with the fix (publish them to an index).

## How coverset runs pytest

```text
collect: python -m pytest --collect-only -q -p no:cacheprovider --rootdir=<repo> --coverset-out=<dir>
         --coverset-shapes=off --continue-on-collection-errors <testpaths...>
shard:   python -m pytest @<shard.args> --coverset-out=<dir> --coverset-scores=<file> --coverset-source=<pkg>...
         --continue-on-collection-errors -p no:cacheprovider -q [--numprocesses=0] [--no-cov] --rootdir=<repo>
```

`--rootdir` is the repository, so pytest uses the repository's own configuration file (`pytest.toml`,
`pytest.ini`, `pyproject.toml`, `tox.ini` or `setup.cfg`, the first that applies), its `addopts`,
markers, `filterwarnings` and plugins. Words of `addopts` that clash:

| In `addopts` | What happens | Fix |
|---|---|---|
| `--lf`, `--ff`, `--nf`, `--sw`, `--cache-clear` | usage error: the cache plugin is disabled | remove it from `addopts` (the doctor prints what the rest should read) |
| `-n`/`--numprocesses` without pytest-xdist in the environment | usage error | add pytest-xdist to the test dependencies, or remove it from `addopts` |
| `--cov*` without pytest-cov in the environment | usage error | the same |
| `-x`, `--maxfail` | a shard stops at its first failure and the rest of it never runs | remove (coverset's fail-fast already reports the first failure) |
| `-n` with pytest-xdist, `--cov*` with pytest-cov | neutralised (`--numprocesses=0`, `--no-cov`) | nothing to do |

## What the doctor checks

| Code | Level | Meaning |
|---|---|---|
| `not-python` | error | no Python project found (a `package.json` gets the upload-mode hint) |
| `config` | error | `[tool.coverset]` or a file it reads is invalid |
| `environment` | error | no environment source, or the resolver failed (message and fix from uv) |
| `no-source` | error | no source packages inferred: coverage would measure nothing |
| `testpaths` | error | configured test paths that do not exist |
| `addopts` | error/warning | an `addopts` word from the table above; the fix edits the repository's `addopts` (which also fixes local pytest runs; `pytest_args = ["--override-ini=addopts=..."]` would fix coverset's runs only) |
| `lfs-local-packages` | error | local packages stored with Git LFS |
| `import` | error | `import <package>` fails from `PYTHONPATH` in the built environment |
| `collect-usage`, `collect-timeout`, `no-tests` | error | pytest stopped, timed out or collected nothing |
| `collect-errors` | error/warning | test modules that fail to import (an error when nothing was collected) |
| `no-lock` | warning | the environment is resolved, not locked |
| `direct-references` | warning | URL/VCS requirements: untrusted pull requests that change the environment are never built |
| `compiled` | warning | compiled extensions: re-snapshot after merging native changes |
| `large-repo` | warning | over 20,000 files or 200 MB as `git archive` exports them (Git LFS files count as pointers); the largest files are named |
| `no-testpaths`, `unknown-keys`, `python` | warning | pytest collects from the root; keys this version ignores; `requires-python` excludes 3.12+ |
| `env-note`, `extras`, `xdist`, `pytest-cov`, `no-pytest`, `install`, `local-packages`, `console-scripts`, `root-conftest`, `invariants`, `many-tests` | info | what coverset adjusted or will do, with the fix where one applies |

Collection uses the LocalExecutor exactly as a local run would: the cached venv for the environment's
hash (built on first use), the project and local packages installed if needed, the working tree
(tracked files plus untracked ones git does not ignore) put into a throwaway worker, an `import` of
every source package, then the planner's collect command with the plugin. It runs the repository's
`conftest.py` and test modules' imports as your user, as `pytest --collect-only` would.

## Onboarding a mirror of a public repository

```sh
git clone --mirror https://github.com/pallets/click /tmp/click.git && git clone /tmp/click.git ~/src/click
coverset doctor --repo ~/src/click --write-config     # read the findings, then the suggested section
# add the section to the mirror's pyproject.toml (it is your fork), commit, push
coverset warm --repo you/click                         # environment image + first trusted snapshot
```

Measured on this machine (macOS, warm uv cache): `coverset doctor --no-collect` takes about 1.5 s on
the demo app and on a 2,352-file Flask application (a 239-package `uv.lock`); with collection, the demo
app takes 1.6 s (97 tests) and the Flask application 78 s (5,426 tests collected in 47 s, the
environment ready in 2 s from the uv cache, its two committed wheels installed in 2 s). Describing an
environment (`PythonAdapter.env_spec`, done for every run) takes 22 ms for the demo app, about 50 ms
for the Flask application and about 200 ms for a large uv workspace; a requirements resolution takes
about 0.4 s online and nothing once cached.
