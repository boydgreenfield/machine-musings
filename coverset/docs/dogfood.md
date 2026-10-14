# Dogfood: coverset measured by coverset

coverset's own suite run under `pytest --coverset -n 6`, and the bundle read back with `coverset report` and `coverset tests`. The first run (pass D1) was of commit `150a1c0` (a `git archive` of it, so the analysis did not move while `src/coverset` was being edited) with the plugin fixes below; the numbers here are from the re-run on the working tree after the deletion passes (D2, D3) that acted on it. The same steps work on any checkout.

## The command

```sh
pytest -q -n 6 -p no:cacheprovider --timeout 900 -m 'not slow and not modal and not network' \
  --coverset --coverset-source=coverset --coverset-out=DIR --coverset-neutral
```

Write every `--coverset-*` value as one word (`--coverset-out=DIR`). The two-word form `--coverset-out DIR` works while `DIR` does not exist yet; once it exists (the second run) and lies outside the project, the plugin stops with exit 4 and the one-word form, because pytest would read `DIR` as a test path and lose `pyproject.toml` ([instrumentation.md](instrumentation.md#value-options-are-one-word---optvalue)).

## The numbers

3,072 tests collected, 75 deselected (`slow`, `modal`, `network`), 2,997 run, on this Mac (Apple silicon, Python 3.13.0, Postgres 14), 2026-10-14. (At `150a1c0`: 3,078 collected, 3,003 run.)

| | plain `-n 6` (`150a1c0`) | `--coverset -n 6` (`150a1c0`) | `--coverset -n 6` (after D3) |
|---|---|---|---|
| outcomes | 3,002 passed, 1 failed | 3,002 passed, 1 failed | 2,996 passed, 1 failed |
| pytest's own time | 118.7 s | 136.8 s (1.15×) | 135.6 s |
| wall time | 119.8 s | 160.9 s (1.34×) | 159.0 s |

The failure in every column was the control test `tests/analysis/test_analysis_test_command.py::test_the_split_form_loses_the_configfile`, which asserted the old silent loss of `pyproject.toml` under `--coverset-out run/out`; the plugin now refuses that command line (bug 1 below). It is now `test_the_split_form_is_refused` and asserts exit 4 and the one-word hint, so the suite passes.

The 23 s after the tests: merging six worker directories 13.7 s (`meta.timings.finalize.merge`; `coverage.json` 6.3 s and repository paths 2.8 s of it), the neutral export 6.9 s, `bundle.tar.gz` 1.8 s. The bundle: 2,997 tests, 181 files of `src/coverset`, 3,318 functions traced (1,211 reached the sample cap), `coverage.sqlite` 107 MB, `bundle.tar.gz` 36 MB, `neutral.tar.gz` 12 MB, `meta.errors` empty. `coverset report` wrote 6,440 files in about 29 s; they take 721 MB, 468 MB of it the 2,997 test pages (up to 600 KB each).

Before the fixes the same command gave 178 failed, 2,372 passed and 713 errors in 119 s, and the bundle said nothing was wrong.

## Two bugs this run found in the plugin

**1. xdist workers lost the project's configuration** (`--coverset-out DIR` as two words). pytest looks for its rootdir and configuration file before entry-point plugins have registered their options, so `DIR`, the value of an option it does not know yet, is a test path to it whenever `DIR` exists. The controller started fine (`DIR` did not exist yet), created `DIR`, and then started its workers, which parse the same command line again: every worker took the scratch directory as its rootdir, found no `pyproject.toml`, and ran without `testpaths` (so it collected `examples/`, `fixtures/repos` and `packages/*/tests`: the 59 collection errors), without `asyncio_mode = "auto"` (every async fixture failed; the 637 `assert not self._finalizers` errors are pytest-asyncio strict mode tearing down those fixtures) and without `addopts`. The two `bringing up nodes...` lines at the top of the log are xdist's own (`-q` prints it twice in a plain run too). Fixed: the controller hands its workers every `--coverset-* VALUE` pair as one word (command line and `PYTEST_ADDOPTS`), and any run (serial, controller) whose rootdir or configuration file such a word moved stops with exit 4 naming the one-word form; a value inside the project still passes. Tests: `test_a_separate_value_word_keeps_the_workers_configuration[argv|PYTEST_ADDOPTS]`, `test_a_value_word_inside_the_project_changes_nothing`, `test_glue_values_joins_every_coverset_value_word`.

**2. The merge did not compare the workers' configuration with the controller's.** The broken run wrote a bundle with `meta.errors` empty and the controller's `configfile`, although every test had run under another rootdir and no configuration file. The merge already compared working directories; it now compares each worker's `rootdir` and `configfile` too, records a difference in `meta.errors` (`xdist worker gw0: rootdir . with configuration file .../other.ini, not the controller's ...`) and keeps the worker directories. Test: `test_workers_with_another_configuration_are_an_error`.

What was suspected and is not a bug: the environment. In an instrumented run `COVERSET`, `COVERSET_EVENT_NONCE` and `COVERSET_XDIST` are removed in the plugin's first hook, before conftest files load; a pytest started by a test on a worker inherits `PYTEST_XDIST_WORKER` but not the token, so it is an ordinary run, and in-process runs (`pytest.main`, pytester) stay inert without options of their own. `test_tests_that_run_pytest_stay_out_of_the_run` pins this: under `-n 2 --coverset-out=out`, tests that start pytest in subprocesses (plain, instrumented, with their own workers) and in-process leave a clean outer bundle with exactly the outer tests; it passes on the old plugin as well.

One more finding, in the suite: three cases of `tests/plugin_conformance/test_shape_key_grammar.py::test_computed_keys_are_in_the_grammar` had ids of 5,000 to 11,000 characters (the repr of a huge value), above the neutral format's 4,096-character cap, so the neutral export dropped them (`meta.neutral.dropped.tests_long_ids: 3`) and every reader of the neutral bundle saw 3,000 tests. Their ids are now the first 60 characters and a digest.

## Functions no test executed

From the neutral bundle: a function or method of `src/coverset` whose body statements (`functions.jsonl.gz` `body_start`..`end_line`, intersected with the file's statements) contain no line any test executed (`coverage.jsonl.gz`, every context including `<import>`). 106 of 3,253 functions and methods, 686 statements, in 43 modules (`coverset tests economy` counts 120 of 3,187 "untested" with its own rules). At `150a1c0` it was 138 of 3,334 (894 statements). The twelve Alembic revisions under `src/coverset/db/migrations` are not in the bundle at all: Alembic loads them by path, outside the `coverset` package that coverage measures.

Every entry is code **no test reaches in-process**. That is not the same as dead: code run only in a subprocess (CLI commands the tests start with `subprocess`, `python -c "from coverset... import serve_child"`, the dev reloader's child), only with a live Modal token, or only with the Rust toolchain (`-m slow`) is not measured in the parent. Each entry was cross-checked with a reference search over `src`, `tests`, `scripts`, `docs`, `deploy` and `pyproject.toml`: a public module function counts as used where it is imported and used, reached as `module.name`, used in its own module, named in a subprocess command string or as `python -m`; a private one in its own module and `tests/`; a method by attribute access (`.name`) or as a string; a nested function inside its enclosing function. Definitions, imports, re-exports and `__all__` entries are not uses. Method names shared with other classes (`run`, `name`, `key`) are reported as referenced; the JSON says so in `note`.

**Nothing calls: none left.** The 16 functions nothing referenced at `150a1c0` are deleted (pass D2: `RunContext.nodeid_of`, `adapter_language`, `_infer_packages`, `iter_rust_files`, `iter_profiles`, `Comparison.files_of`/`functions_of`, `SettingSpec.editable_at`, `group_missing`; pass D3: `prior_regions_by_key`, `Settings.bind_port`, `problems_summary`, `report.economy.as_json`, `report.facts.regions_for`, `web.render.html.join`, `web.render.markdown.plain`).

### Reached only out of process

60 functions: 21 run in a subprocess or the server process, 5 are CLI commands no test invokes in-process, 20 need live Modal, 14 need cargo and llvm. They are used; the gap is that the parent's coverage cannot see them.

| Function | Statements | Reached by |
|---|---:|---|
| `agent/mcp_server.py:243` `serve_stdio` | 4 | a subprocess or the server process |
| `cli.py:118` `_serve` | 10 | a subprocess or the server process |
| `cli.py:244` `main` | 1 | a subprocess or the server process |
| `commands/demo.py:84` `seed_demo_data` | 2 | a subprocess or the server process |
| `commands/demo.py:193` `_demo` | 31 | a subprocess or the server process |
| `commands/demo.py:206` `_demo.<locals>.on_run` | 2 | a subprocess or the server process |
| `commands/demo.py:235` `_replay` | 5 | a subprocess or the server process |
| `commands/demo.py:246` `_serve_with_replay` | 21 | a subprocess or the server process |
| `commands/demo.py:271` `_static_report` | 17 | a subprocess or the server process |
| `commands/dev.py:400` `Reloader.run` | 14 | a subprocess or the server process |
| `commands/dev.py:403` `Reloader.run.<locals>.terminate` | 1 | a subprocess or the server process |
| `commands/dev.py:423` `run_reloader` | 3 | a subprocess or the server process |
| `commands/dev.py:444` `serve_child` | 13 | a subprocess or the server process |
| `commands/dev.py:462` `_serve_child` | 15 | a subprocess or the server process |
| `commands/dev.py:482` `_exit_with_parent` | 5 | a subprocess or the server process |
| `commands/dev.py:596` `port_open` | 6 | a subprocess or the server process |
| `commands/facts.py:87` `shapes_command` | 2 | a CLI command no test invokes in-process |
| `commands/invariants.py:112` `invariants_scan_command` | 4 | a CLI command no test invokes in-process |
| `commands/maintenance.py:41` `repo_purge_command` | 2 | a CLI command no test invokes in-process |
| `commands/maintenance.py:133` `modal_reap_command` | 2 | live Modal only |
| `commands/mcp.py:123` `_store_service` | 10 | a subprocess or the server process |
| `commands/mcp.py:133` `_store_service.<locals>.close` | 1 | a subprocess or the server process |
| `commands/mcp.py:139` `serve_command` | 11 | a subprocess or the server process |
| `commands/mcp.py:155` `serve_command.<locals>.main` | 7 | a subprocess or the server process |
| `commands/mcp.py:169` `tools_command` | 4 | a subprocess or the server process |
| `commands/modal.py:42` `modal_smoke_command` | 11 | live Modal only |
| `commands/modal.py:65` `_Steps.__init__` | 1 | live Modal only |
| `commands/modal.py:68` `_Steps.__call__` | 8 | live Modal only |
| `commands/modal.py:82` `_Steps._print` | 2 | live Modal only |
| `commands/modal.py:87` `_smoke` | 53 | live Modal only |
| `commands/modal.py:104` `_smoke.<locals>.build` | 5 | live Modal only |
| `commands/modal.py:116` `_smoke.<locals>.create` | 2 | live Modal only |
| `commands/modal.py:125` `_smoke.<locals>.python_v` | 4 | live Modal only |
| `commands/modal.py:141` `_smoke.<locals>.roundtrip` | 5 | live Modal only |
| `commands/modal.py:150` `_smoke.<locals>.signal_exec` | 8 | live Modal only |
| `commands/modal.py:172` `token_info` | 13 | live Modal only |
| `commands/modal.py:186` `token_info.<locals>.query` | 4 | live Modal only |
| `commands/modal.py:195` `modal_images_command` | 11 | live Modal only |
| `commands/run.py:20` `run_command` | 9 | a CLI command no test invokes in-process |
| `commands/run.py:133` `warm_command` | 2 | a CLI command no test invokes in-process |
| `commands/rust.py:95` `run_command.<locals>.log` | 2 | cargo and llvm only (`-m slow`) |
| `executors/modal_.py:187` `SandboxSlots.free` | 2 | live Modal only |
| `executors/modal_.py:945` `ModalExecutor.worker_for` | 4 | live Modal only |
| `executors/modal_.py:1406` `ModalExecutor.stale_images` | 10 | live Modal only |
| `executors/modal_.py:1527` `ModalExecutor._write_later` | 8 | live Modal only |
| `executors/modal_.py:1683` `ModalWorker.sandbox` | 1 | live Modal only |
| `executors/modal_images.py:64` `import_modal` | 6 | live Modal only |
| `lang/rust.py:935` `RustAdapter.read_bundle` | 12 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:205` `rustc_version` | 2 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:281` `TestCase.test_id` | 1 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:286` `TestCase.key` | 1 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:591` `_binary_id` | 5 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:666` `_run_libtest.<locals>.run_one` | 17 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:711` `_package_name` | 4 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:793` `_objects` | 4 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:801` `_export` | 33 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:824` `_export.<locals>.export_one` | 7 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:882` `_write_capture` | 8 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:883` `_write_capture.<locals>.rel` | 3 | cargo and llvm only (`-m slow`) |
| `lang/rust_llvm.py:1211` `repo_root_of` | 6 | cargo and llvm only (`-m slow`) |

**The in-process pattern.** A command is cheap to measure when the test calls it through Click's `CliRunner` instead of a subprocess: `runner.invoke(cli, [...])` runs the command function in the test's own process and context. `tests/test_settings_scopes.py::test_the_settings_commands_set_read_and_clear` does this for `coverset settings set|get|list|clear|keys` (five commands and their helpers: 12 functions, 103 statements that no test ran before): it points `COVERSET_DATA_DIR` and `COVERSET_DATABASE_URL` at the `settings` fixture's directory and database with `monkeypatch`, clears `get_settings`'s cache, and stays synchronous because the commands call `asyncio.run` themselves. `coverset run`, `warm`, `repo purge`, `invariants scan` and `facts shapes` can follow the same pattern; the dev reloader, `coverset demo`, `coverset mcp serve` and the Modal and Rust paths cannot, by design.

### Reachable but untested

46 functions that tested code can call but no test drives: error branches, `__repr__`/`__eq__`, fork handlers.

| Function | Statements |
|---|---:|
| `agent/context.py:269` `RunContext.median_duration` | 2 |
| `agent/docs.py:121` `main` | 9 |
| `agent/remote.py:125` `_code` | 1 |
| `commands/common.py:72` `_call.<locals>.stop` | 3 |
| `commands/deploy_cmd.py:96` `_profile_has_token` | 2 |
| `commands/doctor.py:598` `_collect` | 54 |
| `commands/doctor.py:650` `_collect.<locals>.on_line` | 3 |
| `commands/doctor.py:678` `_first_line` | 4 |
| `commands/doctor.py:797` `_extract` | 2 |
| `commands/facts.py:256` `_suggestions` | 13 |
| `db/testing.py:49` `server_problem` | 9 |
| `db/testing.py:54` `server_problem.<locals>.probe` | 1 |
| `executors/local.py:227` `LocalExecutor.ensure_snapshot` | 1 |
| `executors/local.py:782` `uv_project_installer` | 18 |
| `executors/local.py:867` `_uv` | 7 |
| `facts/store.py:194` `_after_fork` | 5 |
| `facts/store.py:319` `FactsStore.__repr__` | 1 |
| `forge/base.py:87` `RepoRef.owner` | 1 |
| `forge/base.py:92` `RepoRef.name` | 1 |
| `forge/fake.py:318` `FakeForge.hide_comment` | 4 |
| `forge/github_events.py:294` `_installation_repositories` | 8 |
| `forge/oidc.py:130` `_jwk_client` | 6 |
| `forge/registry.py:166` `LiveForge.__getattr__.<locals>.call` | 1 |
| `lang/python_env.py:1302` `_edge_marker` | 5 |
| `lang/python_env.py:1753` `_default_cache_dir` | 2 |
| `orchestrator/bundles.py:336` `shared_ingest_pool` | 4 |
| `orchestrator/bundles.py:344` `_record_failure` | 2 |
| `orchestrator/planner.py:744` `_collect_failure` | 2 |
| `orchestrator/runner.py:1030` `_Execution._expire_pr_runs` | 7 |
| `orchestrator/upload.py:262` `_pack_dir` | 4 |
| `report/build.py:222` `FileEntry.hollow` | 1 |
| `report/economy.py:635` `git_reader.<locals>.read` | 1 |
| `report/economy.py:767` `_with_base_tests` | 2 |
| `settings_scopes.py:549` `_Unset.__repr__` | 1 |
| `storage/__init__.py:102` `_after_fork` | 6 |
| `storage/cachedir.py:84` `EvictReport.line` | 7 |
| `storage/cachedir.py:95` `_gb` | 1 |
| `storage/local.py:97` `LocalBlobStore.__repr__` | 1 |
| `storage/local.py:100` `LocalBlobStore.__eq__` | 1 |
| `storage/local.py:103` `LocalBlobStore.__hash__` | 1 |
| `storage/s3.py:137` `S3BlobStore.__repr__` | 1 |
| `storage/s3.py:151` `S3BlobStore.forget_client` | 2 |
| `storage/s3.py:422` `S3BlobStore.local_path` | 2 |
| `web/setup.py:289` `_orphan` | 4 |
| `web/uploads.py:152` `_repo_by_name` | 4 |
| `web/uploads.py:160` `_dev_user_refused` | 2 |

## Redundant-test candidates

`coverset tests redundant` reports 143 groups of identical flows (784 tests) and 662 dominated tests; `coverset tests minimal` keeps 1,215 of 2,997 tests and calls 1,782 removable candidates (216.1 s of 685.1 s summed test time). (At `150a1c0`: 151 groups, 651 dominated, 1,236 kept of 3,003.) The assertions of every candidate were compared mechanically (the `assert` statements and `pytest.raises` calls of each test function, normalised by `ast.unparse`) and the closest pairs read by hand.

* **275 tests measure nothing of their own.** Two autouse fixtures in `tests/conftest.py` run in every test's context (`clear_registered_secrets()`, `logging.py:102-104`, and `configure_caches(None)`, `facts/cache.py:100`). A test that touches no other line of `src/coverset` (138 of `tests/plugin_conformance`, which exercise the plugin and `coverset_model`, neither measured with `--coverset-source=coverset`; `tests/model`; benchmarks) has exactly that flow, so they form one group of 254 and one of 21, and all 275 are "dominated" and "removable". The economy should treat lines that nearly every test runs as no flow (it already has `empty_tests` for tests with none); here they hide 275 tests from `empty_tests: 0`.
* **105 groups are one parametrized family** (362 cases): different inputs through the same lines, such as the 40 redaction cases (`tests/test_redaction.py`, every case one alternative of the same regular expressions), git ref formats, shape keys. Coverage sees one line; the inputs are the test. None is removable on coverage alone.
* **36 groups and 262 dominated pairs join different test functions.** In 80 of the group pairs and 236 of the dominated pairs the assertions differ outright (no assertion shared, but for three pairs that share one); in 21 and 26 the test has no assertion of its own (the checks are in a helper, e.g. `_expect_integrity_error` for six database constraints, or in the plugin conformance helpers); one group pair is a subset (`test_hs256_key_confusion_rejected` of `test_alg_none_rejected_before_key_lookup`, both `security`: kept). A second comparison that also follows each test into the same-module helpers it calls finds no further subset: the largest overlaps (19 of 25 checks in `tests/plugin_conformance/test_neutral_conformance.py`, 3 of 6 in `tests/test_smoke_hosted.py`) are the same checks on different fixtures.

Read by hand (`redundant.json` `read_by_hand` has 18 verdicts):

| Candidate | Kept by | Verdict |
|---|---|---|
| `tests/db/test_engine.py::test_the_statement_timeout_cancels_a_long_statement` | `test_lifting_the_statement_timeout_lasts_one_transaction` | **redundant**, deleted in pass D3: the second ends by asserting the same cancellation on the same 0.2 s engine |
| `tests/test_app.py::test_non_api_errors_are_not_json`, `test_v1_errors_are_json` | `test_security_headers_on_every_response[/no-such-page]`, `[/v1/nope]` | keep: the same request, disjoint checks (merging would put JSON checks into a `security` test) |
| `tests/forge/test_forge_api.py::test_hs256_key_confusion_rejected` | `test_alg_none_rejected_before_key_lookup` | keep: one allowlist, two attacks; each catches a different widening |
| `tests/forge/test_forge_api.py::test_valid_token` | `test_leeway_accepts_a_just_expired_token` | keep: the leeway is a PyJWT argument, visible only through the input |
| `tests/test_models.py::test_migrate_is_idempotent` | `test_migrations_match_models` | keep: different properties of the schema |
| `tests/facts/test_facts_queries.py::test_connection_has_external_access_off_and_locked` | `test_connection_reads_files_only_inside_the_facts_directory` | keep: two DuckDB settings set by one `connect()` |
| `tests/analysis/test_analysis_sharding.py::test_a_forced_shard_count_ignores_the_small_suite_cap` | `test_guessed_durations_never_make_shards_of_fewer_than_three_tests` | keep: the cap and its override are one `min()` |
| others in `read_by_hand` | | keep: same code, different data or a different property |

So one test of the 1,767 removable candidates at `150a1c0` was truly redundant (now deleted), and none of the 1,782 now; coverage-equal is not assertion-equal in this suite, which tests data-driven code (regular expressions, SQL constraints, library arguments) more than branches.

## The minimal covering set

1,215 of 2,997 tests (41 %) keep every line, arc and input class that the suite covers in `src/coverset`; they take 468.9 s of the 685.1 s summed test time (68 %). It is a coverage bound, not a suite: it drops all 275 floor-only tests (whose subjects are not measured), 1,507 tests whose facts others also cover, and with them every assertion they make.

## Reproducing it

```sh
# a fixed snapshot of the commit (any checkout works the same way)
SNAP=/tmp/coverset-snap; OUT=/tmp/coverset-dogfood
mkdir -p "$SNAP" && git archive HEAD | tar -x -C "$SNAP"
cp -R tests/lang/data "$SNAP/tests/lang/"          # git-ignored test data
(cd "$SNAP" && git init -q && git add -A)          # seed_demo lists the demo's files with git
export PYTHONPATH="$SNAP/src:$SNAP/packages/coverset-model/src:$SNAP/packages/pytest-coverset/src"
cd "$SNAP"

# plain, then instrumented
time python -m pytest -q -n 6 -p no:cacheprovider --timeout 900 -m 'not slow and not modal and not network'
time python -m pytest -q -n 6 -p no:cacheprovider --timeout 900 -m 'not slow and not modal and not network' \
  --coverset --coverset-source=coverset --coverset-out="$OUT/.coverset" --coverset-neutral

# the analysis
coverset report "$OUT/.coverset" --repo-root "$SNAP" --out "$OUT/report"
for c in economy redundant minimal; do
  coverset tests $c "$OUT/.coverset" --repo-root "$SNAP" --json > "$OUT/tests-$c.json"
done
```

`python` and `coverset` are the project environment's (`.venv/bin`); `PYTHONPATH` puts the snapshot's packages in front of the editable installs. The re-run after D3 used the live checkout instead of a snapshot (`cd coverset` and the instrumented command with `--coverset-out=$OUT/.coverset`, `--repo-root ..` for the report); there the git root is the directory above, so bundle paths start with `coverset/` and that prefix comes off before the script below. The never-executed list from the neutral bundle:

```python
import gzip, json, collections
rows = lambda name: [json.loads(l) for l in gzip.open(f"{OUT}/.coverset/neutral/{name}", "rt")]
statements = {r["path"]: set(r["statements"]) for r in rows("files.jsonl.gz")}
executed = collections.defaultdict(set)
for r in rows("coverage.jsonl.gz"):
    executed[r["file"]].update(r["lines"])
for f in rows("functions.jsonl.gz"):
    body = {n for n in statements[f["path"]] if f["body_start"] <= n <= f["end_line"]}
    if f["kind"] in ("function", "method") and body and not body & executed[f["path"]]:
        print(f'{f["path"]}:{f["def_line"]} {f["qualname"]}')
```

For the next pass, next to the bundle: `never_executed.json` (`path`, `line`, `qualname`, `referenced_elsewhere`, plus `category`, `references`, `note`) and `redundant.json` (every group and dominated pair with the mechanical verdict, the fixture floor, and `read_by_hand`).
