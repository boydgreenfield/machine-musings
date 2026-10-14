# Production flows: input classes production sees and tests never pass in

coverset already answers "which kinds of argument did the tests pass to this function, and which kinds did they never pass" ([input-classes.md](input-classes.md)). That answer is relative to an *expected* set the catalogue derives from the observed values and the annotations. Production traffic is a better reference: it says which kinds of value really arrive, and how often. A class production sends every minute that no test has ever passed in is the most useful gap coverset can report; a class no test passes and production never sends is a much weaker one.

This page is the design for feeding production back into coverset, and the record of the prototype built on 2026-10-13. Status words follow [ARCHITECTURE.md](../ARCHITECTURE.md): **built** means the code exists on branch `boyd/testing` and has tests; **designed, not built** means only this page describes it.

| Part | Status | Where |
|---|---|---|
| Runtime probe: the shape tracer in a long-running process, sampling, k-anonymity gate, windows, exporters, WSGI/ASGI middleware, overhead self-check | built | `packages/coverset-probe/` |
| Payload format `prod-flows` v1 | built | `docs/schema/prod-flows.v1.json`, `coverset.facts.flows.PROD_FLOWS_SCHEMA` |
| Importer and storage (`coverset flows import`, the `prod_*` facts tables) | built | `src/coverset/facts/flows.py`, `src/coverset/commands/flows.py` |
| Gaps: production classes no test passed in, ranked by call volume, with Hypothesis hints (`coverset flows gaps`) | built | `src/coverset/analysis/archetypes.py` (`production_gaps`) |
| Archetypes: clustering of call vectors, closed frequent itemsets, covering tests (`coverset flows archetypes`) | built (prototype) | `src/coverset/analysis/archetypes.py` |
| `POST /v1/flows` | designed, not built | the `HttpExporter` already speaks it |
| OTLP span attributes for flows, Sentry and Datadog integrations | designed, not built | this page |
| UI (production toggle on the shape grid, Flows page, PR verdict line), MCP tools | designed, not built | this page |

## Goals and non-goals

Goals:

- **Production as the reference.** For every function production calls, list the input classes production passes in that no test of the reference run did, with how much traffic each carries.
- **Never handle values.** Production values are personal data, secrets, customer content. The probe exports categorical *shape keys* only: the type, sign, length bucket and string kind of a value, never the value, a substring of it, a hash of it or its repr.
- **Cheap enough to leave on.** About 0.1 µs per call of an observed function, a hard per-window budget, and a self-check that switches the probe off when it costs more than its budget.
- **Standard transport.** The probe's output must travel through pipelines operators already run (OpenTelemetry collectors, files shipped by a log agent), not only through a coverset-specific endpoint.
- **Function-level coverage of production.** Which functions production calls that no test calls, and which request flows go through them.

Non-goals:

- **Line-level coverage in production.** Running coverage.py in production costs too much and its output (which lines ran) is not what is missing; function-level call presence is in scope, line-level coverage is not.
- **Capturing or replaying traffic.** No request bodies, no record-and-replay, no "golden" production samples. Test data comes from Hypothesis strategies built from the classes ("Mapping to tests").
- **Anomaly detection or APM.** The probe counts kinds of input. It is not a profiler, an error tracker or a latency monitor; it rides alongside those.
- **Other languages tonight.** The probe is Python's `sys.monitoring`. The payload is language neutral (shape keys from [neutral-bundle.md](neutral-bundle.md#shape-keys)); a Node probe would emit the same format.

## What is captured

Per export window (60 s by default), per process:

| Row kind | Key | Values | Purpose |
|---|---|---|---|
| `funcs` | module, qualname, first line, path relative to `source_root` (when known) | exact call count, sampled calls, whether the per-window cap stopped sampling | identity, call volume, function-level presence |
| `rows` | (function, parameter, shape key), including `return` and `<receiver>` | estimated calls, distinct traces (or subjects), estimated calls per hashed caller | the gap computation |
| `calls` | (function, call vector): every parameter's shape key of one call | estimated calls, distinct traces | archetypes (which classes arrive *together*) |
| `flows` | the set of functions one sampled request called | distinct traces | function-level coverage of whole requests |

A **flow** is everything one trace did. The middleware (or `coverset_probe.trace()` for jobs) opens a context per request from the W3C `traceparent` header; a head-sampled trace records every call it makes (up to `max_calls_per_trace`, 256), so its flow is complete. The trace id itself never leaves the process: it is hashed with a per-process salt into a 64-bit *witness* used only for counting distinct traces.

**Callers** are exported as `blake2b-8(module \0 qualname)`, 16 hex digits, or `<external>` for calls from outside the allow-listed modules (the web framework, a worker loop), with at most 8 callers per row (the rest under `<other>`). The importer recomputes the same hash over every function region of the reference run (`caller_index`), so it can name callers without the probe ever shipping internal names to a third-party collector in clear. The hash hides names from casual readers of the collector's data, not from someone holding the source: it is a convenience, not a secret.

**What a shape key reveals.** Exactly the features listed in [neutral-bundle.md](neutral-bundle.md#shape-keys): for an int its sign and whether it exceeds 2^31; for a float its sign, NaN, infinity and integrality; for a string its length bucket (`0`, `1`, `2-10`, `11-100`, `101-1000`, `>1000`), ASCII or not, whitespace-only, leading/trailing whitespace, whether it contains a digit, and its kind (`email/plain`, `email/multi-label-domain`, `quantity/decimal`, `numeric`, `iso-date`, `url`, ...); for containers the length bucket, element type, numeric summaries (any zero, any negative, all zero, sums to zero), order and duplicates; for other objects the class name. Nothing in a key is derived from a value's characters beyond those categories.

## Privacy and anonymisation

| Layer | Rule | Where |
|---|---|---|
| Categorical by construction | Only shape keys leave a callback; production mode has no example reprs at all (the plugin's `example_repr` is never imported) | `coverset_probe/tracer.py` |
| Grammar check before export | Every key is validated against the version-1 grammar (`coverset_model.shape_keys.validate`); a key that does not parse is dropped and counted (`probe.invalid_keys`), so a feature bug cannot smuggle free text out | `coverset_probe/window.py` |
| Allow-list of modules | `modules=[...]` is required; only functions whose module is (under) one of them are observed; the probe never observes itself | `ProbeTracer._allowed` |
| Parameter opt-out by name | Parameters matching `exclude_params` are never observed (default: the plugin's `SECRET_NAME_RE`, `pass|secret|token|key|auth|cookie|session|cred`, case-insensitive) | `ProbeTracer._new_target` |
| k-anonymity | A `(function, parameter, shape)` row, a call vector or a flow is exported only after **k distinct witnesses** (default 5) showed it, counted over the process lifetime; a row below k is held back and counted (`probe.suppressed`), and its counts from earlier windows are never published | `KGate` |
| Witnesses | The salted hash of the *subject* when the application names one (a user or tenant id, via `subject=` on the middleware), else of the trace id; outside any trace each sampled call is its own witness | `coverset_probe/context.py` |
| Differential privacy (optional) | `dp_epsilon=ε` adds Laplace noise to every exported count: scale 1/ε on traces, `max_calls_per_trace`/ε on calls (the most one trace can contribute); counts are clamped at zero | `build_payload` |
| Hashed callers | see above | `caller_hash` |
| Retention | Imported windows live under `repo=<id>/prod/`; `coverset flows prune --older-than-days N` (default 30) deletes batches whose newest window is older; `coverset repo purge` removes them with the repo | `prune_windows` |
| Importer re-checks | The control plane validates every window as untrusted input (size caps, JSON Schema, indices, grammar) and stores `k_ok` per row; readers skip rows that are not `k_ok` | `coverset.facts.flows` |

What k-anonymity buys here, honestly: a shape is published only once several independent requests (or users, with `subject=`) produced it, so a rare category that could single out one user ("someone passed a 1,000+ character Unicode string to `rename`") stays in the process. What it does not buy:

- **Untraced calls count calls, not people.** Without the middleware, one client calling five times passes k = 5. Use the middleware with `subject=` wherever a user is known.
- **The gate decides on true counts**, so with noise on, the *fact* that a key passed the gate is not differentially private; only the published counts are. Treat ε as protection of volumes, k as protection of rare categories.
- **Per-process counting is conservative.** Each worker of a pre-fork server counts its own witnesses, so the effective k across the fleet is higher, never lower.
- **Function call counts are exported as is.** They describe code, not people, and are the same numbers any APM already collects.

## Transport

### The payload (`prod-flows` v1)

One JSON object per window. The JSON Schema is [schema/prod-flows.v1.json](schema/prod-flows.v1.json) (generated from `coverset.facts.flows.PROD_FLOWS_SCHEMA`; a test keeps them equal).

```json
{
  "version": 1,
  "kind": "coverset.prod-flows",
  "service": "ledger-api",
  "env": "production",
  "release": "demo-main",
  "window": {"start": "2026-10-13T10:00:00Z", "end": "2026-10-13T10:01:00Z"},
  "probe": {"version": "0.1.0", "features": "vendored", "python": "3.13.0", "sample_rate": 0.01,
            "trace_sample_rate": 0.01, "k": 5, "window_s": 60, "dp_epsilon": null,
            "sampled_calls": 734, "sampled_returns": 501, "suppressed": {"rows": 10, "calls": 6, "flows": 0},
            "pending_keys": 16, "dropped_keys": 0, "invalid_keys": 0, "budget_hit": false,
            "overhead": 0.0004, "fast_call_ns": 84.1, "errors": 0, "export_errors": 0,
            "disabled": null, "shed": []},
  "funcs": [{"module": "ledger.accounts.emailcheck", "qualname": "validate_email", "line": 8,
             "path": "src/ledger/accounts/emailcheck.py", "calls": 253, "sampled": 129, "capped": false}],
  "rows": [{"f": 0, "p": "email", "s": "str|ascii|kind:email/multi-label-domain|len:11-100",
            "calls": 40.0, "traces": 20, "callers": {"<external>": 40.0}}],
  "calls": [{"f": 0, "v": [["email", "str|ascii|kind:email/multi-label-domain|len:11-100"]], "calls": 40.0, "traces": 20}],
  "flows": [{"fs": [0, 3, 5], "traces": 93}]
}
```

`f` indexes `funcs`. `calls` are Horvitz-Thompson estimates (each recorded call weighs 1/p of the probability it was recorded with), so they are floats; `funcs[].calls` is exact. A producer that applies its own k gate says so with `probe.k`; rows of a producer that does not are `k_ok` only with at least 5 traces in their own window. A row may carry an explicit `k_ok`.

### Shipping it

| Transport | Status | How |
|---|---|---|
| JSON lines on disk | built | `JsonlExporter(path)`: one window per line, file mode 0600; ship it with the log agent you have, then `coverset flows import FILE.jsonl[.gz] --repo NAME` |
| Direct HTTP | client built, endpoint designed | `HttpExporter(url, token)`: gzipped POST with `Authorization: Bearer`, https only (plain http only when the parsed host is exactly `localhost`, `127.0.0.1` or `::1`, so `http://localhost.attacker.example` is refused), up to 3 windows queued across failures. `POST /v1/flows` would authenticate the bearer as a repo-scoped upload token (the upload agent's token model), run exactly `import_payloads` per window and answer 202 with the import counts |
| OpenTelemetry metrics | built | `OtelMetricsExporter(meter)`: a counter `coverset.shape.calls` (unit `{call}`), one `add` per row with attributes `service.name`, `deployment.environment`, `code.namespace`, `code.function`, `coverset.param`, `coverset.shape_key`. Any OTel collector forwards it; cardinality is bounded by the number of distinct shape keys, which the k gate and `max_keys` cap. Call vectors and flows do not fit a counter and are not sent this way |
| OTel span attributes (flows) | designed, not built | At the end of a sampled trace, the middleware sets `coverset.flow` (the sorted function list) and `coverset.flow.shapes` (the call vectors, as shape keys) on the *existing* server span when `opentelemetry-api` is present, so a tracing backend's own sampling and retention apply. The k gate cannot apply per span; this mode is opt-in and for environments whose tracing backend is already trusted with request metadata. A collector-side processor would then aggregate spans into `prod-flows` windows |

## Sampling

Two modes, chosen per call by whether a trace context is set:

- **Per trace (head sampling).** The decision is made once when the request starts. With a `traceparent` header it follows OpenTelemetry's `TraceIdRatioBased` rule (keep when the low 64 bits of the trace id are below `rate × 2^64`), so every service running the probe at the same rate keeps the same traces and their flows line up. Without a header the decision is random. Within a kept trace every call of an observed function is recorded, up to `max_calls_per_trace`.
- **Per call (outside any trace).** Each function counts its calls and records the next one when the counter reaches its next sample point: geometric gaps, i.e. Bernoulli sampling at the function's current rate.

Caps, all reset at every window:

| Cap | Default | Effect |
|---|---|---|
| adaptive rate | `settle=64` | after 64 recorded calls of a function with no new `(parameter, shape)`, its per-call rate is divided by 4, down to `sample_rate/64`; a new shape restores the base rate. Weights (1/p) keep the estimates unbiased |
| `max_samples_per_func` | 1,000 per window | the function stops recording until the next window (`funcs[].capped`); its exact call count goes on |
| `max_obs_per_window` | 20,000 per window | the process-wide hard budget: every function stops recording (`probe.budget_hit`) |
| `max_keys` | 20,000 | distinct rows, vectors and flows per window; new keys beyond are dropped and counted (`probe.dropped_keys`). Each keeps at most 64 witnesses (`traces` saturates at 64), which bounds a window's memory to about 70 MB in the worst case; the k gate holds at most 50,000 pending keys of k witnesses |
| shedding | a quarter of `max_overhead` | a function whose unsampled calls alone cost more than this is dropped for the rest of the process (`probe.shed`) |
| `max_overhead` | 2 % of one core | above it, the probe switches itself off and says why (`probe.disabled`) |

**Windows and processes.** Every probe process exports its own windows: a flush thread closes one every `window_s`, and an `atexit` hook registered by `start()` exports the last partial window on a normal interpreter exit (a recycled worker, a deploy, a batch job shorter than `window_s`; not `os._exit` or a fatal signal, so call `stop()` in a server's worker-exit hook where one exists). Each worker of a pre-fork server therefore writes interleaved windows, and the readers pool by time, not by count: `--windows N` takes, per service, every window that ended within N typical window lengths (the median duration) of the newest one, whichever process wrote it, and `--minutes M` every window that ended in the last M minutes. With one process and full-length windows `--windows N` is its newest N windows.

Capping never uses `sys.monitoring.DISABLE` (re-enabling a disabled location needs `restart_events()`, which would affect every other monitoring tool in the process). Only shedding and the final switch-off, which are permanent, do.

## Overhead budget and how it is measured

Every call of an observed function runs the *fast path*: one dict lookup, a counter increment, one `ContextVar.get` and a compare. `PY_RETURN` is switched on for a code object only while one of its sampled calls is in flight, so an unsampled call costs one callback, not two.

Measured with `packages/coverset-probe/bench/bench_overhead.py` (a 10,000-call loop, best of 7, Python 3.13.0, Apple M4 Max, 2026-10-13):

| Function | No probe | Probe at 1 %, no trace | Inside an unsampled trace | Added | One recorded call |
|---|---|---|---|---|---|
| `noop(x)` (returns its argument) | 15 ns | 94-101 ns | 97-99 ns | 79-85 ns | 2.5 µs |
| `check(email)` (one regex match) | 106-115 ns | 207-212 ns | 199-204 ns | 94-101 ns | 3.7-4.1 µs |
| `render(row)` (`json.dumps` of a small dict) | 1.29-1.37 µs | +0.04-0.18 µs (noise) | +0.08 µs | under 0.2 µs | 6.6 µs |

So: **about 0.1 µs per call of an allow-listed function, plus about 3-5 µs per recorded call** (at 1 %, 30-50 ns amortised). The recorded call is dominated by computing shape keys and by switching `PY_RETURN` on and off (about 1 µs of it; `record_returns=False` saves that). A service whose observed functions take 10 µs or more pays about 1 %; a tight loop in an allow-listed module pays far more, which is what shedding and the self-check are for: allow-list the domain layer, not utilities called in inner loops.

The self-check estimates the probe's own cost every window as `callbacks × fast_call_cost + time inside recorded calls`, where `fast_call_cost` is calibrated at start (the measured extra time of an unsampled call under `sys.monitoring`, on a throwaway function, about 85 ns here) and recorded calls are timed directly. It reports the result as `probe.overhead` (a fraction of one core) and `probe.fast_call_ns`. It does not see cache effects on the application's own code, so the bound is set low and the benchmark is the reference.

## Clustering into archetypes

A single class tells you *that* production sends subdomain addresses; an archetype tells you *in which combination* (subdomain address, on the `team` plan, from the EU region), which is what a test should reproduce.

- **Items.** Each call vector becomes an *itemset*: the union of the input classes of its arguments, named by the catalogue (`email:multi-label-domain`, `plan:literal 'team'`, `ids:duplicates`). Classes, not shape keys: `len:2-10` versus `len:11-100` is noise for this purpose.
- **Common items are set aside.** Items present in at least 95 % of the calls (`email:str`, `email:ascii`) are removed before clustering and added back to every core; left in, they make any two calls look half alike and Jaccard distance would merge exactly the archetypes worth telling apart.
- **Clustering.** Distinct itemsets, weighted by estimated calls, are clustered bottom-up with average linkage on Jaccard distance (Lance-Williams updates, pure Python), merging while the closest pair is within 0.5 and beyond that while more than 8 clusters remain. Only the 150 heaviest itemsets are clustered; lighter ones join the nearest cluster or stay out. k-modes would serve too; agglomerative clustering needs no k and is deterministic.
- **Archetype.** A cluster's *core* is the items present in at least half of its weight; its *name* is its distinguishing items (frequency inside the cluster minus frequency outside, at least 0.25, top three); a cluster with nothing beyond the common items is the *baseline*.
- **The other view: frequent itemsets.** Closed frequent itemsets (Apriori, weighted support ≥ 5 %, up to three items) list the co-occurring classes without forcing a partition; `coverset flows archetypes --patterns` prints them.
- **Stability across windows.** An archetype's id is a hash of its sorted core, so the same behaviour keeps its id window after window (tested on two independent synthetic samples). When a core drifts (an item crosses the 50 % line), `match_archetypes` pairs it with the previous window's archetype of highest core Jaccard (at least 0.5, greedy). The designed `prod_archetypes` table stores ids per window so the UI can show an archetype's history.

## Mapping to tests

For each function the gap computation (`production_gaps`) puts the production shapes and the reference run's test shapes through **one** catalogue call, marking each shape with a pseudo test id, so both sides get identical class names (including the catalogue's collision renames). Then:

- **Gaps.** Classes production passed in that no test did, each with its share of the parameter's production calls, the estimated calls (scaled to the function's exact call count), the largest trace count among its shapes and the catalogue's Hypothesis hint. A *type* class (`existing_domains:set` when tests only pass lists) is a gap only for a parameter the tests pass at all; for a parameter no test passes, its specific classes say more.
- **Priority** is call volume × gap: functions are ranked by the estimated production calls that fall in untested classes.
- **Covering tests per archetype.** A test's classes for the function are the union over its calls (the facts aggregate per test, not per call). *Exact*: restricted to the archetype's parameters, the test's classes equal the core. *Superset*: every core class and more. *Partial*: some, at least one of the archetype's own (sharing only the common classes is no evidence). An archetype with none is *untested*.
- **Hypothesis strategies.** Every gap carries the catalogue's hint for its class. An archetype that is not exactly covered gets an `@given(...)` built from the most specific class per parameter (email kinds before string features before the type), e.g. `@given(email=st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"]), existing_domains=st.sets(st.text()))`.

On the demo (the fixture `fixtures/flows/ledger-prod.jsonl`, three windows of synthetic traffic through the demo app under the real probe, against the demo's `main` baseline):

```
$ coverset flows gaps --repo coverset-demo/ledger --windows 3
check_if_new_user_signup_allowed  src/ledger/accounts/signup.py:11  ~787 calls · 3 of 10 production classes never in tests
  existing_domains:set      100.0%  ~    787 calls  st.sets(st.text())
  email:multi-label-domain   14.1%  ~    110 calls  st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"])
  email:idn                   1.6%  ~     12 calls  st.just("bob@bücher.de")
...
validate_email  src/ledger/accounts/emailcheck.py:8  ~787 calls · 2 of 8 production classes never in tests
  email:multi-label-domain   14.1%  ~    111 calls  st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"])
  email:idn                   1.6%  ~     12 calls  st.just("bob@bücher.de")
...
called in production, never by a test:
  src/ledger/util/text.py:initials  ~661 calls
```

And per call, the archetypes of `validate_email` with the tests that cover each:

```
$ coverset flows archetypes --repo coverset-demo/ledger --windows 3 --func validate_email
validate_email  src/ledger/accounts/emailcheck.py  ~754 sampled calls
  every call: email:ascii + email:str
  [db7b228c]  69.0%  exact     email:plain
      covered by tests/test_emailcheck.py::test_validate_email_returns_the_domain[alice@example.com-example.com] (exact), ...
  [2e3147c9]  14.1%  untested  email:multi-label-domain
      @given(email=st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"]))
  [54595429]   9.8%  exact     email:plus-tag
  [55c14c91]   5.6%  exact     email:uppercase
  [b32fad19]   1.6%  partial   email:idn + email:unicode
      @given(email=st.just("bob@bücher.de"))
```

The subdomain class is the demo's PR #2 bug ([input-classes.md](input-classes.md#validate_emailemail-str---str)): the tests never pass it, production sends it in one signup in seven, and the vendor pattern rejects it.

## Mapping coverage

Function-level, in scope and built: `coverset flows gaps` lists the functions production called that no test of the reference run reached, with their call volume, and the request flows that went through them (traces summed over windows). Line-level coverage of production is out of scope (see Non-goals): the probe never records lines, and the answer "which lines ran in production" is better approximated by joining these functions with the run's per-test line coverage than by instrumenting production.

"Reached" is decided from the run's line coverage, not from its shapes (`run_functions_reached`): a body line some test ran, read from the `func_body` summary when it is fresh, else from `test_lines` (an `<import>`-only hit does not count). A function with no `call_shapes` row can still be tested: a run made with `--coverset-shapes off`, a tracer that could not claim its `sys.monitoring` tool id, or a zero-parameter generator (no argument and no return shape). Class gaps do need shapes, so `gaps` and `archetypes` refuse a reference run whose `call_shapes` is empty and ask for another with `--run`, rather than report every production class as a gap.

## Integrations

| System | Design | Status |
|---|---|---|
| OpenTelemetry | metrics exporter (built, above); span attributes for flows (designed, above); the probe reads `OTEL_SERVICE_NAME` for its service name | partly built |
| Sentry | Sentry's "local variables" capture records *values*, which is exactly what the probe avoids. Instead: a Sentry SDK `before_send` hook (or event processor) that, for each in-app frame of the exception's stack, computes shape keys of the frame's arguments with the probe's feature code and attaches them as `contexts.coverset.frames[i] = {function, params: {name: shape_key}}`, then strips nothing else (the SDK's own scrubbing still runs). A coverset importer pulls issues through Sentry's API, maps frames to function regions with the same module/qualname resolution, and marks classes as **seen in a crash, never tested**, ranked above plain gaps | designed, not built |
| Datadog | a `ddtrace` trace filter (`TraceFilter`) that, for spans of sampled traces, adds `coverset.flow` the same way as the OTel design; the metrics side uses DogStatsD with the same counter name and tags, or the OTel exporter through Datadog's OTLP intake | designed, not built |
| Generic | `JsonlExporter` plus any log shipper, then `coverset flows import` | built |

## UI

Designed, not built (the web layer belongs to other work tonight):

- **Function page, shape grid: a source toggle** `tests | production | both`. In *both*, each cell has three states with non-colour cues: tests only (hollow square, as today's "observed"), production only (filled triangle, "production, never tested", the cell that matters), both (filled square). Production cells show share of calls and traces on hover/focus; keyboard: `p` cycles the source.
- **A Flows page per repo**: archetypes per function, sorted by untested share × volume; each row shows the archetype name, share, status (exact, superset, partial, untested) with its covering tests linked to the test page, and the `@given` snippet with a copy button. Windows and services are filters; the archetype id links to its history across windows.
- **PR verdict line**: when a PR changes functions with production data, the sticky comment adds one line, e.g. "this PR changes 2 functions whose production archetypes are untested: `validate_email` (subdomain addresses, 14 % of calls), `parse_duration` (multi-letter units, 21 %)". Computed from the newest windows of the default branch's service; advisory, never a failure.
- **Standalone report**: `coverset report` gains a production column in the data-flow lens when the repo has windows.

## Data model

Production data lives beside the runs, never inside a run directory, so run views, `coverset gc` and the per-run inventories never see it:

```
<facts_dir>/repo=<repo_id>/
  dictionary.json                  shape keys get ids here, shared with call_shapes
  prod/
    .lock                          imports of one repo serialise on it
    service=<service>/
      batch=<utc stamp>-<sha12>/   one per imported file and service; renamed into place whole
        prod_windows.parquet  prod_funcs.parquet  prod_shapes.parquet  prod_calls.parquet  prod_flows.parquet
```

Schemas are `PROD_SCHEMAS` in `src/coverset/facts/schema.py` (kept out of `SCHEMAS`/`TABLES`). Every table starts with `window_start`, `window_end`, `service`, `env`; function tables add `func_key` (null when no region matched at import), `module`, `qualname`, `path`, `line`.

| Table | Further columns | Notes |
|---|---|---|
| `prod_windows` | `release`, `payload_sha256`, `imported_at`, `k`, `sample_rate`, `probe_json` | one row per window; `payload_sha256` (canonical JSON) makes re-imports no-ops |
| `prod_funcs` | `calls`, `sampled`, `capped` | exact call counts: function-level presence |
| `prod_shapes` | `param`, `shape_id`, `calls`, `traces`, `k_ok`, `callers_json` | the gap input; `shape_id` is the repo dictionary's id, so it joins `call_shapes.shape_id` directly |
| `prod_calls` | `vector_json`, `calls`, `traces`, `k_ok` | the archetype input |
| `prod_flows` | `funcs_json`, `traces`, `k_ok` | function sets of sampled traces (func keys, or `module:qualname` when unresolved) |
| `prod_archetypes` | `func_key`, `archetype_id`, `core_json`, `name`, `share`, `calls`, `traces`, `status`, `run_id` | **designed, not built**: computed per window at import once the UI needs history; today `coverset flows archetypes` computes them on demand |

**Joining windows and runs.** A window is compared with a *reference run*: `--run`, else the newest completed run of the default branch with facts. Functions are matched by `func_key` when the import resolved one that the run also has, else re-resolved by `(path, qualname)` or module name (`FunctionResolver`, built once per read and reused for every function; the module must match the dotted names exactly one file can be imported as; same-qualname regions are told apart by the closest first line). Lambdas are traced in production but never indexed as regions, so they are counted (`unresolved_lambdas`), not listed. `release` is recorded so a later version can pick the run whose head is the deployed commit.

**Scale.** Measured on this machine with synthetic windows: 80,000 rows (2,000 functions; 9 MB of JSON) validate in 0.13 s and import in 0.43 s; 800,000 rows (92 MB) import in 4.7 s at 2.8 GB peak RSS, read back in 3.4 s, and the gaps of 20,000 functions take 5.5 s. Those figures are for windows resolved at import; windows imported before any run existed are resolved at read time through one index (`FunctionResolver`): 2,000 unresolved functions against 20,000 regions resolve in well under a second (an earlier per-function rebuild of the index took 74 s; a test bounds it). The row check is hand-written (20× faster than `jsonschema`; the JSON Schema still checks the envelope, and a test holds both to the same verdicts). Past this, the path is the facts store's own: stream rows instead of loading a window whole, aggregate windows into daily batches, and do the class join in DuckDB over `prod_shapes` and `call_shapes` (they share shape ids).

## MCP tools

Designed for the MCP server (another agent's work tonight), read-only, returning the same JSON as the CLI's `--json`:

| Tool | Arguments | Returns |
|---|---|---|
| `production_gaps` | repo, optional function, run, windows, service | the `flows gaps` report: classes production sends that no test does, with shares and strategies |
| `production_archetypes` | repo, function, run, windows | archetypes with status, covering tests and `@given` snippets |
| `production_examples` | repo, function, archetype id | **synthetic** example values for an archetype, generated from its classes' strategies (never production values, which coverset does not have), for bootstrapping fixtures and test data |
| `production_presence` | repo, list of functions | call volume per function and whether any test reaches it, for "is this code even used" questions while refactoring |

These give an agent revising a PR the evidence to write one property test for the archetype production actually sends instead of ten example tests for classes it never sees, and the evidence to delete tests of paths production and the remaining tests both leave alone.

## Rollout plan and risks

1. **Tonight (done):** probe, payload, importer, gaps, archetypes, all on the demo fixture and synthetic data.
2. **First real service:** run the probe in staging with `export=JsonlExporter(...)`, `modules=[<domain package>]`, `sample_rate=0.01`, the middleware with `subject=` set; import daily with `coverset flows import`; check `probe.overhead`, `probe.suppressed` and `probe.shed` in the payloads before production.
3. **`POST /v1/flows`** with repo-scoped upload tokens, so `HttpExporter` replaces the file shipping.
4. **UI:** the shape-grid toggle first (it reuses the catalogue output), then the Flows page and the PR verdict line.
5. **Integrations:** OTel span attributes, then Sentry crash classes, then Datadog.
6. **Unify the feature code.** `coverset_probe/_features.py` is a copy of `pytest_coverset/features.py` (the probe must not depend on pytest, and the plugin's imports were another agent's to change tonight). Both should import one dependency-free module, most naturally `coverset_model.features`; until then two parity tests (`packages/coverset-probe/tests/test_features_parity.py` when the plugin is installed, `tests/test_flows_cli.py` always) hold them byte-identical.

Risks:

| Risk | Mitigation |
|---|---|
| Overhead on hot paths | allow-list domain modules only; shedding; the self-check; the benchmark before rollout |
| Feature drift between probe and plugin (production keys that never match test keys) | parity tests now, one shared module next; the comparison is by class, not key, so length-bucket changes do not create false gaps |
| A shape key leaking data | categorical features only, grammar validation on both sides, no reprs, parameter opt-out, k gate |
| Rare but important classes held back by k | lower k per service for staging; the suppressed counts in every payload say how much was held back |
| Function identity drift after refactors | resolution by module/qualname with the closest line; unresolved functions are listed, not guessed |
| `sys.monitoring` tool ids in use (a profiler, coverage in production) | the probe tries ids 4, 5, 2 and reports why it could not start; it never displaces another tool |
| Free-threaded Python | counters are plain attributes: under free threading a few increments can be lost (counts are estimates anyway); dicts are thread-safe; no correctness issue beyond that |

## Trying it

```sh
# probe tests (standalone: the probe is not a workspace member and has no dependency but coverset-model)
uv run --isolated --no-project --python 3.13 --with pytest \
  --with-editable ./packages/coverset-model --with-editable ./packages/coverset-probe \
  pytest packages/coverset-probe/tests
# the overhead benchmark
uv run --isolated --no-project --python 3.13 --with-editable ./packages/coverset-model \
  --with-editable ./packages/coverset-probe python packages/coverset-probe/bench/bench_overhead.py
# on a demo data directory (coverset demo --executor local --no-serve)
uv run coverset flows import fixtures/flows/ledger-prod.jsonl --repo coverset-demo/ledger
uv run coverset flows gaps --repo coverset-demo/ledger --windows 3
uv run coverset flows archetypes --repo coverset-demo/ledger --windows 3 --patterns
```

`--with-editable` rather than `--with`: uv caches a path dependency's wheel by its version, so `--with ./packages/coverset-probe` keeps running a stale build after an edit. The control-plane side is covered by `tests/facts/test_flows.py`, `tests/analysis/test_archetypes.py` and `tests/test_flows_cli.py` in the main suite.
