# Repository fixtures for environment sources

Tiny Python repositories, one per way a project can describe its test environment, used by
`tests/executors/test_env_sources.py`, `tests/executors/test_local_executor_install.py` and
`tests/test_doctor.py`. Each is complete enough for `coverset doctor --repo fixtures/repos/<name>`.

| Fixture | Environment source | Layout | Install (auto) |
|---|---|---|---|
| `../../examples/demo-app` | `uv.lock` | src | none |
| `requirements-only` | `requirements.txt` + `requirements-dev.txt` (`-r`, `-e .`, an index option) | flat | none |
| `pyproject-only` | `[project]` dependencies, an extra, a `test` dependency group | src (hatch) | none |
| `poetry` | `poetry.lock` (lock format 2.1, fabricated: no file hashes) | flat | none |
| `setup-cfg-only` | `setup.cfg` `[options]` | flat | none |
| `setup-py-only` | `setup.py` keyword literals, read with `ast` | flat | none |
| `src-entry-points` | `[project]` + `[dependency-groups]` | src (setuptools) | wheel: entry points, `importlib.metadata` at import, package data |

The `requirements-docs.txt` beside `requirements-only` is deliberately not picked up (docs files
are skipped by auto-detection).
