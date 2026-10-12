# Extending coverset

coverset has four seams, each a `typing.Protocol`: where tests run (`Executor`), where code is hosted (`Forge`), which language is under test (`LanguageAdapter`), and who reviews (`ReviewClient`). This page says what each seam expects and where to register a new implementation. The last section covers new input-class features.

| Seam | Protocol | Registered in | Existing implementations |
|---|---|---|---|
| executor | `coverset.executors.base.Executor`, `Worker` | `coverset.executors.get_executor` | `LocalExecutor`, `ModalExecutor` |
| forge | `coverset.forge.base.Forge` | `coverset.forge.registry` | `GitHubForge`, `FakeForge` |
| language | `coverset.lang.base.LanguageAdapter` | `OrchestratorContext.adapter` | `PythonAdapter` |
| reviewer | `coverset.ai.client.ReviewClient` | `coverset.ai.client.get_review_client` | `FakeReviewClient`, `AnthropicReviewClient` |

## Adding an executor

An executor builds environments and hands out workers; a worker is one isolated place to run commands. The method-by-method contract table is in [docs/orchestrator.md](orchestrator.md#writing-a-new-executor-backend). This section adds what the orchestrator assumes around it.

### The protocols

```python
class Executor(Protocol):
    name: str

    async def ensure_env(self, spec: EnvSpec) -> EnvRef: ...
    async def ensure_snapshot(self, env: EnvRef, repo: str, sha: str) -> SnapshotRef | None: ...
    async def acquire(
        self, base: EnvRef | SnapshotRef, n: int, resources: Resources, ttl_s: int, labels: Mapping[str, str]
    ) -> list[Worker]: ...
    def capabilities(self) -> Capabilities: ...
    def cost_usd(self, resources: Resources, seconds: float) -> float: ...
    async def close(self) -> None: ...


class Worker(Protocol):
    id: str
    root: str

    def path(self, rel: str) -> str: ...
    async def run(
        self,
        cmd: list[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout_s: float,
        on_line: LineCallback | None = None,
        stdout_cap: int,
    ) -> RunResult: ...
    async def put_tree(self, tar_bytes: bytes, dest: str) -> None: ...
    async def get_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def cancel(self) -> None: ...
    async def release(self) -> None: ...
```

Optional extensions: `PoolingExecutor` (`fill_pool`, `drain_pool`; used only when `capabilities().warm_pool` is true) and `ReapingExecutor` (`reap(active_run_ids=..., all=...)`, for workers that can outlive the control plane).

### What the orchestrator expects

**Environment.** `EnvSpec` (`lang/base.py`) carries `python`, `requirements_txt` (from `uv export`), `plugin_wheel` (the pytest-coverset wheel), `source_root` and `env_hash = sha256(requirements.txt \0 python \0 sha256(wheel))` (`executors/env_export.py`). Build one environment per `env_hash` and cache it: install the requirements, then the wheel with its dependencies. Never install the project itself; tests import it through `PYTHONPATH`. The LocalExecutor builds `~/.cache/coverset/venvs/<env_hash>` in a staging directory under an exclusive `flock` and renames it into place.

**Layout.** Paths given to a worker are relative to `Worker.root`. The planner uses this layout (`orchestrator/planner.py`, `WorkerLayout`):

| Path | Written by | Contents |
|---|---|---|
| `repo/` | `put_tree` | the head commit, from a `git archive` tarball |
| `run/shard.args` | `write_file` | the shard's node ids in priority order, one per line |
| `run/shard.scores.json` | `write_file` | `{nodeid: score}` reproducing that order |
| `run/full-trace.txt` | `write_file` | paths traced without a sample cap |
| `run/collect/` | pytest | worker 0's collection bundle |
| `run/out/` | pytest | the shard's bundle; `run/out/bundle.tar.gz` is pulled with `get_file` |
| `run/retry-<n>.args`, `run/retry-<n>/` | `write_file`, pytest | one retry's node id and output (never ingested) |
| `run/STOP` | `write_file` | graceful stop under `fail_fast = "cancel_remaining"` |

`run()` executes with `cwd="repo"` and the command line already holding absolute in-worker paths from `Worker.path`.

**Environment variables.** The caller passes `PYTHONPATH`, `COVERSET_EVENT_NONCE` and the run's `extra_env`. Pass them through `sanitize_env` and nothing else: the child's environment must not inherit the control plane's. Set `PATH` to the environment's interpreter, and `HOME` and `TMPDIR` inside the worker.

**Events.** Call `on_line(text, "stdout" | "stderr")` for every line as it arrives, newline stripped, each line cut at `MAX_LINE_BYTES` (1 MiB). The runner finds events by the nonce prefix ([docs/instrumentation.md](instrumentation.md#events-on-stdout)), so stdout must reach it unaltered and promptly: no buffering until exit. Kill the exec when it has written `stdout_cap` bytes and return `truncated=True`. Never raise for a non-zero exit; return the code in `RunResult`.

**Cancel.** `cancel()` must send a graceful signal first. The plugin turns SIGTERM into an interrupt and still writes its bundle ([docs/instrumentation.md](instrumentation.md#stopping-early)); escalate to a hard kill after a grace period (5 s locally). Return `cancelled=True` from the interrupted `run()`. `release()` stops everything, frees the worker and is idempotent.

**Capacity.** `acquire(n)` may return fewer than `n` workers. The planner re-deals the shards over what it got; an empty list fails the run with reason `capacity`. Raise `CapacityError` only when nothing at all can be had. `resources.network` is `blocked` for untrusted runs; honour it or refuse.

**Cost.** `cost_usd(resources, seconds)` is the rate card. The runner sums it over each shard's exec seconds into `runs.cost_estimate`, and the daily budget (`orchestrator/budget.py`) applies to executors in `BUDGETED_EXECUTORS` (today `modal`).

**Labels.** `labels` carry `coverset_run`, `repo` and `role`. Use them to tag remote resources so `reap` can find workers whose run is no longer active.

### Registering it

1. Add the kind to `ExecutorKind` in `config.py` (`Literal["local", "modal"]` today); it is stored in `runs.executor`, a `VARCHAR(32)` with values checked in Python, so no migration is needed.
2. Build it in `coverset.executors.get_executor`, importing lazily so the other paths never need its client library.
3. Add the choice to `coverset run --executor`, `coverset warm --executor` and `coverset demo --executor` (`commands/run.py`, `commands/demo.py`).
4. Test it against `tests/executors/test_local_executor.py` as a model: environment scrubbing, timeouts, caps, file transfer, release.

Designed, not built: a Modal Functions executor with memory snapshots, and Daytona, E2B and Docker executors. Each is one more `Executor` behind the same registration.

## Adding a forge

A forge verifies and parses webhooks, hands out short-lived mirror credentials, and posts results.

### The protocol

```python
class Forge(Protocol):
    name: str

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool: ...
    def parse_event(self, headers: Mapping[str, str], raw_body: bytes) -> ForgeEvent: ...
    async def fetch_mirror_credentials(
        self, installation_id: int | None, repo: RepoRef
    ) -> MirrorCredentials: ...
    async def create_check(
        self,
        repo,
        head_sha,
        name,
        *,
        external_id,
        details_url,
        status="queued",
        conclusion=None,
        output=None,
        actions=(),
    ) -> int: ...
    async def update_check(
        self, repo, check_id, *, status=None, conclusion=None, output=None, actions=None, details_url=None
    ) -> None: ...
    async def set_commit_status(self, repo, sha, state, *, context, description, target_url) -> None: ...
    async def upsert_pr_comment(self, repo, pr_number, marker, body, prior_id) -> int: ...
    async def hide_comment(self, repo, comment_id) -> None: ...
    async def collaborator_permission(self, repo, login) -> str: ...
    def capabilities(self) -> ForgeCapabilities: ...
```

`verify_webhook` must check the signature over the raw bytes, in constant time, and return False when no secret is configured. `parse_event` runs only after verification and raises `ForgeEventError` for a malformed payload.

### Event kinds

`parse_event` reduces a delivery to a `ForgeEvent` whose `kind` is one of `ForgeEventKind` (`forge/base.py`). Intake (`orchestrator/intake.py`) acts on the kind, not on the forge's own event names:

| Kind | Intake effect | Fields it needs |
|---|---|---|
| `pr_opened`, `pr_sync`, `pr_reopened`, `pr_ready` | upsert the PR; create a `pull_request` run, gated by trust | `repo`, `pull` (`PullRef`: number, head and base SHA, refs, title, author, `author_association`, `from_fork`, `draft`) |
| `push` | a `baseline` run on the default branch | `repo`, `head_sha`, `base_sha` |
| `rerun` | a `rerun` run copying the commit's last run | `repo`, `head_sha`, `sender` |
| `action_button` | `approve-run`, `rerun-failed` or `rerun-all` | `repo`, `head_sha`, `action_identifier`, `check_external_id`, `sender` |
| `install`, `uninstall` | record repositories or clear their installation | `repos_added`, `repos_removed`, `installation_id` |
| `repo_visibility` | update a known repository's `private` flag | `repo` |
| `ignore` | store the delivery with `reason` | `reason` |

Identify repositories by the forge's stable numeric id (`RepoRef.external_id`), never by a name or a URL from the payload. Check every SHA with `check_sha` and every `owner/name` with `check_full_name`. Map the forge's own trust signal onto `PullRef.trusted`: not from a fork, and an owner, member or collaborator.

### Verdict posting

`forge/verdict.py` (`post_verdict`) drives the posting through `capabilities()`:

- with `check_runs`, it creates the `coverset` check if no id is stored, then updates it with the conclusion, a markdown summary, failure annotations and, with `check_actions`, the buttons;
- else, with `commit_statuses`, it posts a `coverset` status (green only for `success`);
- with `pr_comments`, it upserts one sticky comment marked `<!-- coverset:sticky -->`.

`upsert_pr_comment` must edit only comments the integration wrote itself: the stored id first, then a marker scan limited to the integration's own comments. `collaborator_permission` must return the user's role (`admin`, `maintain`, `write`, `triage`, `read` or `none`) or raise `ForgeError`; the approval flow refuses anything below write. Forge errors never fail a run: posting is best effort and the errors are logged.

`MirrorCredentials` must travel only as `git -c http.extraHeader=...`; `orchestrator/repos.py` handles that once the forge returns a token.

### Registering it

1. Add the kind to `ForgeKind` in `db/models.py` (`Literal["github", "fake", "local"]` today). `repos.forge` and `webhook_deliveries.forge` are `VARCHAR(32)` checked in Python, so no migration is needed.
2. Return the adapter from `forge/registry.py` (`get_forge`, `forge_mode`) and resolve its route name in `webhook_forge`, so `POST /webhooks/<name>` reaches it. The access guard already treats every `POST /webhooks/*` as public, so the adapter's `verify_webhook` is the only gate.
3. Give the adapter an `allowed_events` frozenset of the forge's event names (the `Forge` protocol requires it); `web/webhooks.py` records and answers 202 `ignored_event` for any delivery whose event is not in the route's `forge.allowed_events`, before the payload is parsed.
4. Record signed fixtures the way `scripts/make_fixtures.py` does for GitHub, and test parsing, signatures and intake against them (`tests/forge/test_forge_events.py`, `test_forge_intake.py`).

Designed, not built: `forge/gitlab.py` and a Bitbucket adapter.

## Adding a language

A new language does not need coverset to run its tests. The repository's own test runner runs them with a reporter that writes a **neutral bundle**, and coverset ingests it like a Python shard: the facts store, patch coverage, tests per line, the treemap, input classes, the pages, the sticky comment and the static report all read the same model.

1. **Emit the neutral bundle** specified in [docs/neutral-bundle.md](neutral-bundle.md): `manifest.json` with your `language` and `producer`, `tests.jsonl`, `files.jsonl`, `coverage.jsonl`, and for a language coverset cannot parse, `functions.jsonl` (function regions with their hashes and type descriptors); `arcs.jsonl`, `shapes.jsonl` and `links.jsonl` when you have them. The JSON Schemas are in [docs/schema/](schema/).
2. **Check it** with `coverset bundle validate PATH` (every problem with file, row, field and message; exit 1 when there is any) and `coverset bundle info PATH` (rows per file, and what the reader would drop or clip). `fixtures/bundles/neutral-ts/` is a complete, valid TypeScript example, written by `scripts/make_neutral_fixture.py`.
3. **Register the language name** with the neutral adapter: `coverset.lang.neutral.register_language("go")` (`typescript` and `javascript` are registered already). `coverset.lang.adapter_for_language(name)` then returns a `NeutralAdapter`, and `coverset.lang.read_any_bundle` reads the bundle whatever its language, because the manifest says `coverset-neutral`.

`NeutralAdapter` implements the `LanguageAdapter` protocol (`coverset/lang/base.py`) from the bundle alone: `read_bundle` is `coverset_model.io.read_bundle` under the untrusted-bundle limits, `index_functions` returns the shipped regions of a path, `changed_functions` compares the shipped regions of the base and head runs by qualname, ordinal and hashes (without base regions, every head region the diff touches is `modified`), and `shape_catalogue` reads the shipped type descriptors. `env_spec` and `test_command` raise `NotRunnableError`: coverset does not build the environment or start the runner for these languages, so the anticipatory scheduling of [docs/orchestrator.md](orchestrator.md) (first-test latency, likeliest failures first, sharding) stays Python-only; ingest, facts, pages and reports do not. Per path, the run's function regions are the adapter's own index of the head sources when it has one, else the regions the bundle shipped ([docs/data-model.md](data-model.md#the-neutralbundle-contract)); a job whose language is `python` never takes shipped regions or types, whatever the bundle's manifest says.

### The TypeScript walk-through

The TypeScript adapter is written: `packages/coverset-vitest` is a Vitest reporter that produces every file of the bundle from a real run, `examples/ts-app` uses it, and `tests/e2e/test_vitest_reporter.py` runs the example's `npm test`, validates the bundle (no problem), ingests it like a shard and builds the report. [docs/typescript.md](typescript.md) documents it in full; this is how each file is made, as a map for the next language or runner:

| File | How `coverset-vitest` writes it |
|---|---|
| `tests.jsonl` | from the reporter's `onTestRunEnd(testModules)`: `id` is `<test file> > <describe> > <name>` (Vitest's `fullName` behind the repository path), `passed`/`failed`/`skip`/`todo` map to `passed`/`failed`/`skipped`/`skipped`, a `test.fails` test that failed as expected is `xfailed`, a test that never ran is `null`; `fail_sig` hashes the error name and the first stack frame inside the repository, never the message; `message` is scrubbed and cut at 8 KiB; `property` is true for fast-check tests (`fc.assert`/`fc.property` in the test's body, a `property` tag or `meta.property`) (`src/reporter.ts`) |
| `coverage.jsonl` | Vitest's Istanbul provider keeps per-file counters in `globalThis.__VITEST_COVERAGE__`; a setup file (`setupFiles`) diffs them in `beforeEach`/`afterEach` without resetting them, maps every statement whose counter moved through the module's source map to its original first line, and spools `(test, file, lines, arcs)` per worker; everything outside a test is `<import>` (`src/setup.ts`, `src/counters.ts`) |
| `files.jsonl` | the remapped coverage map Vitest hands `onCoverage`: statement first lines, and one `[branch line, target line]` pair per branch location, with coverage.py's targets for an `if` (consequent, `else`, or the statement after the `if`) (`src/branches.ts`) |
| `arcs.jsonl` | the same map's branch counters that are non-zero over the run |
| `functions.jsonl` | regions from the TypeScript compiler API (`ts.createSourceFile`, then the program's checker for types): `Cart.addItem`, `outer.inner`, `rateFor.<anonymous>` with ordinals, `def_line` at the first decorator or `export`, `body_start` at the first statement after a JSDoc, sha256 token hashes of body and signature; no `key` (Node's `crypto` has no 8-byte BLAKE2b; the reader computes it) (`src/regions.ts`, `src/functions.ts`, `src/descriptors.ts`) |
| `shapes.jsonl` | a Vite plugin (`enforce: "pre"`, so before TypeScript is stripped) inserts `__cs$E(...)` at each function's entry and wraps every `return` in `__cs$L(token, value)`, on the original lines and behind `/* istanbul ignore next */` so Istanbul counts exactly the same; the recorder computes shape keys (a port of `pytest_coverset/features.py`, checked against the Python plugin's keys for the same values), callers from its own stack, `<receiver>` classes, redacted examples (enum members as `Currency.EUR`), and the adaptive cap (64 calls with nothing new or 2,048 calls, `cap: "sample"`) (`src/plugin.ts`, `src/instrument.ts`, `src/recorder.ts`) |
| `links.jsonl` | `test("...", { meta: { invariant: "cents_conserved" } })` |
| `manifest.json` | `{"format": "coverset-neutral", "version": 1, "language": "typescript", "producer": {"name": "coverset-vitest", "version": "0.1.1"}, "created_at": ..., "shard": ..., "meta": {"runner": "vitest", "vitest": "5.0.3", "node": ..., ...}}` |

**What a Jest port would change.** Everything that reads values, regions and types (`features.ts`, `redact.ts`, `recorder.ts`, `regions.ts`, `instrument.ts`, `descriptors.ts`, `functions.ts`, `branches.ts`, `counters.ts`) carries over unchanged. A Jest port replaces the three runner-facing pieces:

* the instrumentation hook: Jest has no Vite pipeline, so `instrument()` runs inside a custom `transform` (wrapping `ts-jest` or `babel-jest`: instrument the original TypeScript first, then hand the result and its source map on), or as a Babel plugin placed before `babel-plugin-istanbul`;
* the counters: Jest's Istanbul coverage lives in `globalThis.__coverage__` (`coverageProvider: "babel"`), so the setup file reads that key instead, registered with `setupFilesAfterEnv`, where `beforeEach`/`afterEach` and `expect.getState().currentTestName` give the running test (Jest has no stable task id, so the spool keys rows by `testPath` plus the full name);
* the reporter: a Jest reporter class with `onRunComplete(contexts, results)` instead of `onTestRunEnd`; test ids become `file > describe > name` from `assertionResults[].ancestorTitles` and `title`, outcomes from `status` (`passed`, `failed`, `pending`/`skipped`, `todo`), durations from `duration`, and `files.jsonl` comes from `results.coverageMap` (already remapped by Jest) instead of `onCoverage`.

Jest runs each test file in its own VM context, so `globalThis.__coverset__` is per file, as with Vitest's isolated workers, and the spool format can stay as it is.

**Values to shape tokens.** The shape-key vocabulary is the value vocabulary of [docs/neutral-bundle.md](neutral-bundle.md#shape-keys); `coverset-vitest` computes the same features in JavaScript ([docs/typescript.md](typescript.md#values-to-shape-keys) has the details):

| JavaScript value | Type token | Features |
|---|---|---|
| `true`, `false` | `bool` | `true` / `false` |
| a number with `Number.isInteger(n)`, a `bigint` | `int` | `neg`/`zero`/`pos`, `big` beyond ±2³¹ |
| any other number | `float` | `nan`; `inf` (+ `inf-neg`); else `neg`/`zero`/`pos` (never `integral`: integral numbers are `int`) |
| a string | `str` | `len:<bucket>` in code points, `empty`, `whitespace`, `ascii`/`unicode`, `ws-edge`, `digits`, `kind:...` by the same regular expressions as `pytest_coverset/features.py` (Python's whitespace set and Unicode digits) |
| `null`, `undefined` | `NoneType` | none |
| an array | `list` | `len`, `elem:<type or mixed>`, the numeric summaries, `order`, `dups`, `pair` over the first 32 elements |
| a plain object, a `Map` | `dict` | `len`, `elem` (values), `key` (keys) |
| a `Set` | `set` | `len`, `elem`, numeric summaries |
| `Uint8Array`, `Buffer`, `ArrayBuffer` | `bytes` | `len` |
| a class instance | `obj:<module>.<ClassName>` | none (`dataclass`/`pydantic` are Python-only) |
| a function (a class too), a symbol; a proxy, an object whose prototype is one, a receiver of a proxied class (never looked into; a proxied function observes as `obj:Function`) | `obj:Function`, `obj:Symbol`, `obj:Proxy` | none |

**Types to descriptors.** From the checker's declared type of each annotated parameter and the return type (awaited for an `async` function): `number` → `float` (a branded `number & {...}` too), `bigint` → `int`, `string` → `str`, `boolean` → `bool`, `null`/`undefined`/`void` → `none`, `T | undefined` and optional parameters → a union with `none`, `T[]`/`Array<T>`/`ReadonlyArray<T>`/`Iterable<T>` → `list`, `[A, B]` → a fixed `tuple` (`[A, ...B[]]` a variable one), `Set<T>` → `set`, `Map<K, V>`, `Record<K, V>` and index signatures → `dict`, `Uint8Array`/`Buffer`/`ArrayBuffer` → `bytes`, string, number (integral ones included: `1 | 2 | 3` is a literal, not `float`) and boolean literal unions → `literal`, an `enum` → `enum` with its member names (a subset of its members → a literal of their values), a class → `object` (`<module>.<Name>` when declared in the repository); an interface, type literal or object alias → `dict` with `str` keys (what a plain object observes as), a function type or `typeof SomeClass` → `object` `Function`, `any`/`unknown`/`never`/type parameters → `any`, an unannotated or contextually typed parameter → `null`. An enum value is recorded with an example that names the member as a whole token (`Currency.EUR`; the plugin reads the enum from the parameter's annotation when it is declared in the same file or imported from a relative module) so its members become expected classes; a literal value's example is its JSON form (`"eu"`), which the catalogue matches as it matches a Python repr (`'eu'`). The descriptors are stored with the run's functions at ingest (`functions.types_json`), so the function page and the PR page's cards show the classes that `func_input_cov` counted.

## Adding a review client

A reviewer receives a redacted bundle of facts about the changed functions and returns structured verdicts. Results are advisory ([docs/ai-review.md](ai-review.md)).

```python
class ReviewClient(Protocol):
    name: str

    def review(self, bundle: ReviewBundle) -> ReviewOutcome: ...
```

- `review_changes` (`ai/review.py`) calls `review` once per changed function, with `bundle.for_function(func_key)`, and reuses a stored finding when `(func_key, bundle_hash)` matches. The prompt built from the bundle leaves out the head SHA and PR number (`ai/prompts.py`), and `bundle_hash` ignores them, so an unchanged function keeps its finding across pushes.
- Return a `ReviewOutcome`: the `ReviewResult` (one `FunctionVerdict` per function: `verdict` in `covered`, `partially_covered`, `not_covered`, `abstain`; `confidence`; evidence; missing input classes; suggested tests; invariant findings), its `source`, the model name, token counts and a cost estimate.
- Never raise on a refusal or a truncated answer: return an `abstain` verdict with the reason. Raise `ReviewError` for network or API failures; the run continues without the review.
- Never log the bundle, the prompt, the output or a key.
- Treat everything in the bundle as untrusted data, and say so in the system prompt.

Register it in `ai/client.py` (`get_review_client`), add the mode to `AiMode` in `config.py`, and add its label to `FindingSource` in `ai/client.py` and in `db/models.py` (stored in `review_findings.source`). `tests/ai/test_ai_anthropic_client.py` shows how to test a client against a stubbed SDK without network access.

## Adding input-class features

An input class needs three things: a feature in the shape key (recorded in the worker), a rule that makes it expected, and a rule that marks it observed (both in the catalogue).

1. **Record the feature** in `packages/pytest-coverset/src/pytest_coverset/features.py`. Add a token to the right `_*_features` function. Keep the safety rules: compute only for exact builtin types, never call user code, bound the work. Tokens are sorted into the key, so order does not matter. Add cases to `packages/pytest-coverset/tests/test_features.py`.
2. **Accept it in the bundle readers.** `facts/convert.py` (`is_shape_key`) accepts any feature token without spaces or `|`, so a new feature passes there; a new type token must be added to `_SHAPE_TYPE`. The neutral reader checks the version-1 vocabulary, so add the token to `FEATURES` (and a new type token to `SCALAR_TYPES` or `CONTAINER_TYPES`) in `packages/coverset-model/src/coverset_model/shape_keys.py`, and to the "Shape keys" list of [docs/neutral-bundle.md](neutral-bundle.md#shape-keys).
3. **Make it a class** in `packages/coverset-model/src/coverset_model/catalogue.py`:
   - in `_shape_classes`, map the token to `(group, class)` for an observed value;
   - in `Catalogue._expected_rows`, add the class to the expected set, under a trigger that comes from data or the annotation (never unconditionally: a family should open only when the parameter's kind was seen);
   - in `_CLASS_TEXT`, give it a description and a Hypothesis strategy that produces such values.
4. **Test it** in `tests/analysis/test_analysis_shapes.py` (or `packages/coverset-model/tests/`), and check `render_markdown` and `coverset shapes FUNC --markdown` show it.

Changing an existing token changes shape keys, and with them the ids in every repository's `dictionary.json`. Old runs keep the old keys; `shapes_for_function` pools by `shape_id`, so a renamed token shows as a new shape rather than merging with history.

A whole new string kind (say, phone numbers) goes into `string_kind` in `features.py` (and `STRING_KINDS` in `shape_keys.py`). The order of the checks there is the precedence, and `_STR_KINDS` or a new family in `coverset_model/catalogue.py` turns it into classes.
