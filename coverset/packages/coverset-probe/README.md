# coverset-probe

Which kinds of argument value production passes to your functions, as **shape keys only**: the
pytest-coverset shape tracer, adapted to run inside a long-lived Python service at a low sampling
rate. Per window (60 s by default) it exports k-anonymous counts of `(function, parameter, shape
key)`, of whole call vectors and of per-request flows. It never exports a value, a repr, a hash of
a value or a raw trace id. coverset imports the export and lists the input classes production
passes in that no test does:

```
validate_email  src/ledger/accounts/emailcheck.py:8  ~787 calls · 2 of 8 production classes never in tests
  email:multi-label-domain   14.1%  ~    111 calls  st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"])
```

Python 3.12+ (`sys.monitoring`), no dependencies but `coverset-model` (itself dependency-free). The
design, the privacy model and the measurements are in
[`docs/production-flows.md`](../../docs/production-flows.md).

## Quick start

```python
import coverset_probe

coverset_probe.start(
    modules=["myapp"],  # required allow-list: only these modules are observed
    service="myapp-api",  # default: $OTEL_SERVICE_NAME, else "service"
    sample_rate=0.01,  # per call outside traces; also the trace head-sampling rate
    k_anonymity=5,  # a shape leaves the process only after 5 distinct traces/users
    export=coverset_probe.JsonlExporter("/var/log/myapp/coverset-flows.jsonl"),
    window_s=60,
    source_root="/srv/myapp",  # optional: adds repo-relative paths to function ids
)
# Optional: a daemon thread flushes every window, and an atexit hook exports the last partial
# window on a normal interpreter exit. Call stop() yourself to flush earlier (or with os._exit).
coverset_probe.stop()
```

Then, on the coverset control plane:

```sh
coverset flows import coverset-flows.jsonl --repo myorg/myapp
coverset flows gaps --repo myorg/myapp --minutes 60   # every worker's windows of the last hour
coverset flows archetypes --repo myorg/myapp --func validate_email
```

If tracing cannot start (Python < 3.12, every `sys.monitoring` tool id taken), `start()` returns an
inactive probe whose `disabled_reason` says why; the application is never affected.

## One request, one flow: the middleware

Requests should run inside a trace context so that sampling is per request (a sampled request
records all its calls, so its flow is complete) and k-anonymity counts requests, or users, rather
than calls. The context comes from the W3C `traceparent` header when there is one, so services
running the probe at the same rate keep the same traces. `subject=` names a stable user or tenant
id; it is hashed with a per-process salt at once and only used to count distinct users.

**Flask**

```python
from flask import Flask, request
import coverset_probe
from coverset_probe import JsonlExporter, WsgiMiddleware

app = Flask(__name__)
coverset_probe.start(modules=["myapp"], export=JsonlExporter("/var/log/myapp/flows.jsonl"))
app.wsgi_app = WsgiMiddleware(app.wsgi_app, subject=lambda environ: environ.get("REMOTE_USER"))
```

**Django** (`wsgi.py`, or `asgi.py` with `AsgiMiddleware`)

```python
import coverset_probe
from coverset_probe import JsonlExporter, WsgiMiddleware
from django.core.wsgi import get_wsgi_application

coverset_probe.start(
    modules=["myproject.billing", "myproject.accounts"],
    export=JsonlExporter("/var/log/myproject/flows.jsonl"),
)
application = WsgiMiddleware(get_wsgi_application())
```

Under a pre-fork server (gunicorn, uWSGI) either start the probe in each worker (gunicorn's
`post_fork` hook) or start it before the fork: a fork hook gives every worker its own fresh window
and flush thread (`Probe.after_fork_in_child`), since threads do not survive `fork`. Each worker
counts its own k-anonymity witnesses, which is conservative. Each worker also exports its own
windows; `coverset flows gaps --windows N` pools every worker's windows over the last N window
lengths, and `--minutes M` over the last M minutes. On a normal exit an `atexit` hook exports the
last partial window; servers that leave a worker with `os._exit` skip it, so call
`coverset_probe.stop()` from the server's worker-exit hook (gunicorn's `worker_exit`).

**FastAPI / Starlette**

```python
from fastapi import FastAPI
import coverset_probe
from coverset_probe import AsgiMiddleware, JsonlExporter

app = FastAPI()


@app.on_event("startup")
def start_probe() -> None:
    coverset_probe.start(modules=["myapp"], export=JsonlExporter("/var/log/myapp/flows.jsonl"))


app.add_middleware(AsgiMiddleware)
```

**Background jobs**

```python
with coverset_probe.trace(subject=f"tenant-{job.tenant_id}"):
    run_job(job)
```

Both middlewares and `trace()` are pass-throughs while no probe runs.

## Exporters

| Exporter | What it does |
|---|---|
| `JsonlExporter(path)` | appends one JSON object per window (file mode 0600); ship it with your log agent, import with `coverset flows import` |
| `HttpExporter(url, token)` | gzipped POST with a bearer token (https only; plain http only when the host is `localhost`, `127.0.0.1` or `::1`); keeps up to 3 windows across failures. **`POST /v1/flows` is designed, not built yet**: until it exists use `JsonlExporter` and `coverset flows import` |
| `OtelMetricsExporter(meter=None)` | adds each row's call estimate to the OpenTelemetry counter `coverset.shape.calls` with the function, parameter and shape key as attributes; `pip install coverset-probe[otel]` when you pass no meter |

`export=` takes one exporter or a list. An exporter error is counted in the next payload's
`probe.export_errors`, never raised into the application.

## Options

| Option | Default | Meaning |
|---|---|---|
| `modules` | required | allow-list of module names (a module and its submodules) |
| `exclude_modules` | `()` | carve-outs from the allow-list |
| `sample_rate` | 0.01 | per-call Bernoulli rate outside traces |
| `trace_sample_rate` | `sample_rate` | head-sampling rate of traces |
| `k_anonymity` | 5 | distinct witnesses (traces, or subjects) before a row, vector or flow is exported |
| `exclude_params` | `pass\|secret\|token\|key\|auth\|cookie\|session\|cred` (case-insensitive) | parameters never observed; `None` observes all |
| `dp_epsilon` | None | Laplace noise on exported counts |
| `record_returns` | True | record return shapes of sampled calls (about 1 µs per recorded call) |
| `window_s` | 60 | export period; 0 disables the flush thread (call `probe.flush()` yourself) |
| `settle`, `max_samples_per_func`, `max_obs_per_window`, `max_calls_per_trace`, `max_keys` | 64, 1,000, 20,000, 256, 20,000 | adaptive rate and caps, reset every window |
| `max_overhead`, `min_overhead_wall_s` | 0.02, 1.0 | self-check: above 2 % of one core the probe switches itself off; hot functions are shed first |
| `service`, `env`, `release`, `source_root` | | identity carried in every payload |

## Cost

About 0.1 µs per call of an allow-listed function and 3 to 5 µs per recorded call (Python 3.13,
Apple M4 Max; the full table is in the design doc). Allow-list your domain layer, not helpers that
run in inner loops; the probe sheds any function whose calls alone cost more than a quarter of its
budget and switches itself off above the budget. Measure on your machine:

```sh
uv run --isolated --no-project --python 3.13 --with-editable ./packages/coverset-model \
  --with-editable ./packages/coverset-probe python packages/coverset-probe/bench/bench_overhead.py
```

## Development

The package is deliberately outside the uv workspace (it must not pull the control plane's
dependencies), so its tests run in an isolated environment:

```sh
uv run --isolated --no-project --python 3.13 --with pytest \
  --with-editable ./packages/coverset-model --with-editable ./packages/coverset-probe \
  pytest packages/coverset-probe/tests
```

Add `--with-editable ./packages/pytest-coverset` to also run the parity test of the vendored feature
code (`coverset_probe/_features.py`) against the plugin's. Use `--with-editable`: uv caches a path
dependency's wheel by version, so a plain `--with ./packages/coverset-probe` keeps testing a stale
build after an edit.

| Module | Holds |
|---|---|
| `coverset_probe.tracer` | the `sys.monitoring` core: discovery, fast path, sampled path, caps, shedding, calibration |
| `coverset_probe.window` | per-window aggregates, the k-anonymity gate, noise, the payload |
| `coverset_probe.context` | trace contexts, `traceparent` parsing, ratio sampling, witnesses, `trace()` |
| `coverset_probe.middleware` | `WsgiMiddleware`, `AsgiMiddleware` |
| `coverset_probe.exporters` | `JsonlExporter`, `HttpExporter`, `OtelMetricsExporter` |
| `coverset_probe.probe` | `Probe`, `start()`, `stop()`, `current()`, the flush thread and the overhead self-check |
| `coverset_probe.shapes`, `_features` | shape keys: the plugin's feature code when installed, else the vendored copy |
