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

A Vitest or Jest reporter (`vitest-coverset`, say) writes the files like this:

| File | How |
|---|---|
| `tests.jsonl` | from the runner's test results: `id` is `file > describe > name` (stable across runs), `outcome` maps `pass`/`fail`/`skip`/`todo` to `passed`/`failed`/`skipped`/`skipped`, a test that never ran (bail, shard cancelled) has `outcome: null`; `fail_sig` hashes the error class and the top in-repo stack frame (file and line), never the message; `message` is the error text, redacted, at most 8 KiB; `property: true` for fast-check properties |
| `coverage.jsonl` | per-test lines. With `node:inspector`, open a session, call `Profiler.startPreciseCoverage({callCount: true, detailed: true})` once and `Profiler.takePreciseCoverage()` in the reporter's after-each hook: each take returns the block ranges executed since the previous one, so the delta is that test's coverage; map byte offsets to generated lines and through the source map to source lines, then keep the lines that start a statement. With Istanbul (`babel-plugin-istanbul`, or Vitest's `istanbul` provider), snapshot and reset the `__coverage__` counters around each test instead: the statement map's `start.line` of every statement whose counter moved. Everything before the first test (module loading, top-level `describe` bodies) goes under `<import>`. |
| `files.jsonl` | Istanbul's statement map (first line of each statement) gives `statements`; its branch map gives `branches`, one `[branch line, target first line]` pair per target |
| `arcs.jsonl` | the branch-map targets whose counters are non-zero over the whole run |
| `functions.jsonl` | regions from the TypeScript compiler API (`ts.createSourceFile`, walk `FunctionDeclaration`, `MethodDeclaration`, `ArrowFunction` assigned to a name, `ClassDeclaration`) or tree-sitter-typescript: `qualname` follows the class and nesting path (`Cart.addItem`, `outer.<locals>.inner` style is not required, but one convention per producer), `ordinal` separates overloads, `def_line` is the first decorator or the `function`/method line, `body_start` the first statement of the body, `end_line` the closing brace; `body_hash`/`sig_hash` hash the printed body and signature without comments and whitespace; `types` from the checker (below). `key` may be left out (Node's `crypto` has no 8-byte BLAKE2b; the reader computes it). |
| `shapes.jsonl` | a small instrumentation pass (an SWC or Babel plugin, or a Vite transform) wraps each function's entry and return, computes the shape key of every argument (table below) and of `this` (as `<receiver>`), and aggregates `(function, parameter, shape key, caller)` rows with sample counts, the tests seen, and one redacted `example` (at most 200 characters) each; sampling stops after a cap (say 64 calls without a new shape or 2,048 calls) and the `sampling` row says `truncated: true, cap: "sample"` |
| `links.jsonl` | from a tag or annotation convention the suite already uses for invariants (`test("...", {meta: {invariant: "cents_conserved"}})`) |
| `manifest.json` | `{"format": "coverset-neutral", "version": 1, "language": "typescript", "producer": {"name": "vitest-coverset", "version": "..."}, "created_at": ..., "shard": ..., "meta": {"runner": "vitest", ...}}` |

**Values to shape tokens.** The shape-key vocabulary is the value vocabulary of [docs/neutral-bundle.md](neutral-bundle.md#shape-keys); the reporter computes the same features in JavaScript:

| JavaScript value | Type token | Features |
|---|---|---|
| `true`, `false` | `bool` | `true` / `false` |
| a number with `Number.isInteger(n)` | `int` | `neg`/`zero`/`pos`, `big` beyond ±2³¹ |
| any other number | `float` | `nan`; `inf` (+ `inf-neg`); else `neg`/`zero`/`pos` (and `integral` never, since integral numbers are `int`) |
| `bigint` | `int` | as for numbers |
| a string | `str` | `len:<bucket>` (UTF-16 length is fine), `empty`, `whitespace`, `ascii`/`unicode`, `ws-edge`, `digits`, `kind:...` by the same regular expressions as `pytest_coverset/features.py` |
| `null`, `undefined` | `NoneType` | none |
| an array | `list` | `len`, `elem:<type or mixed>`, the numeric summaries, `order`, `dups`, `pair` over the first 32 elements |
| a plain object, a `Map` | `dict` | `len`, `elem` (values), `key` (keys) |
| a `Set` | `set` | `len`, `elem`, numeric summaries |
| `Uint8Array`, `Buffer`, `ArrayBuffer` | `bytes` | `len` |
| a class instance | `obj:<module>.<ClassName>` | none (`dataclass`/`pydantic` are Python-only) |
| a function | `obj:<module>.Function` | none |

**Types to descriptors.** From the checker's declared type of each parameter: `number` → `float` (or `int` when the reporter knows the values are integral, e.g. a branded `Int` type), `bigint` → `int`, `string` → `str`, `boolean` → `bool`, `null`/`undefined`/`void` → `none`, `T | undefined` and optional parameters → a union with `none`, `T[]`/`Array<T>`/`ReadonlyArray<T>` → `list`, `[A, B]` → a fixed `tuple`, `Set<T>` → `set`, `Map<K, V>` and `Record<K, V>` → `dict`, string or number literal unions → `literal`, an `enum` → `enum` with its member names, classes and interfaces → `object`, `any`/`unknown` → `any`, an unannotated parameter → `null`. `Iterable<T>` and `ReadonlyArray<T>` → `list`, as Python's `Sequence`, `Iterable` and `Collection` are. An enum value is recorded with an example that names the member as a whole token (`Currency.EUR`) so its members become expected classes; a literal value's example may be its JSON form (`"eu"`) or a Python-style repr (`'eu'`), both match. The descriptors are stored with the run's functions at ingest (`functions.types_json`), so the function page and the PR page's cards show the classes that `func_input_cov` counted.

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
