# Benchmarks

Live Modal measurements behind the README numbers paragraph, written by `scripts/bench_modal.py`.
Medians and p90 over the listed repetitions; every value is in seconds unless marked.
The bench suite is **synthetic** (`examples/bench-suite`: 1,200 CPU-bound tests, 173.7 s of drawn
serial work); the cold path is a fresh `debian_slim` sandbox that installs dependencies and runs
plain `pytest` in file order: a fresh sandbox with a plain install, which is what a cold CI runner does.

## Environment

| key | value |
| --- | --- |
| bench_date | 2026-10-11 |
| modal_version | 1.6.1 |
| account | workspace boydgreenfield |
| region | us-central |
| cloud | CLOUD_PROVIDER_UNSPECIFIED |
| cpu | 0.125-2.0 |
| memory_mib | 512-2048 |
| plan_tier | Starter |
| python | 3.13 |
| image_builder_version | 2025.06 |
| repetitions | 5 (image_cold: 1) |
| estimated spend of this benchmark | $0.695 (sandbox lifetimes at the limit rate) |

## Results

| scenario | n | median | p90 | min | max | raw |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| image build, cold (MODAL_IGNORE_CACHE=1) | 1 | 70.78 | 70.78 | 70.78 | 70.78 | 70.78 |
| image build, cached | 5 | 0.46 | 0.50 | 0.42 | 0.51 | 0.51, 0.46, 0.50, 0.44, 0.42 |
| create from env image -> first exec | 5 | 1.16 | 2.16 | 0.87 | 2.36 | 1.16, 2.36, 0.87, 0.88, 1.84 |
|   of which Sandbox.create | 5 | 0.13 | 0.31 | 0.11 | 0.33 | 0.11, 0.33, 0.13, 0.11, 0.28 |
| trusted snapshot build (put_tree, collect, compileall, snapshot) | 1 | 4.01 | 4.01 | 4.01 | 4.01 | 4.01 |
| create from snapshot -> first exec | 5 | 1.02 | 1.42 | 0.69 | 1.69 | 1.69, 0.69, 1.02, 1.02, 0.80 |
|   of which Sandbox.create | 5 | 0.11 | 0.16 | 0.10 | 0.20 | 0.20, 0.10, 0.11, 0.10, 0.11 |
| exec on a pooled sandbox (idle 10 s) | 5 | 0.32 | 0.40 | 0.29 | 0.42 | 0.29, 0.36, 0.31, 0.42, 0.32 |
| bench suite, 1 shard(s): start -> last test | 5 | 181.15 | 188.42 | 179.19 | 192.16 | 182.81, 181.15, 179.19, 179.38, 192.16 |
| bench suite, 1 shard(s): start -> done (incl. ingest) | 5 | 181.87 | 189.49 | 180.00 | 193.08 | 184.11, 181.87, 180.00, 180.16, 193.08 |
| bench suite, 16 shard(s): start -> last test | 5 | 19.63 | 21.17 | 17.08 | 22.19 | 22.19, 17.08, 19.63, 18.11, 19.65 |
| bench suite, 16 shard(s): start -> done (incl. ingest) | 5 | 25.26 | 27.12 | 22.63 | 28.27 | 28.27, 22.63, 25.40, 23.43, 25.26 |
| bench suite, 32 shard(s): start -> last test | 5 | 13.19 | 14.40 | 11.84 | 14.84 | 13.19, 13.74, 11.84, 14.84, 12.75 |
| bench suite, 32 shard(s): start -> done (incl. ingest) | 5 | 22.69 | 25.10 | 20.92 | 25.15 | 22.69, 25.15, 20.92, 25.02, 22.47 |
| cold path, bench suite: total | 5 | 179.31 | 190.63 | 161.63 | 192.21 | 161.63, 188.26, 192.21, 172.70, 179.31 |
| cold path, bench suite: sandbox + install | 5 | 3.84 | 4.13 | 2.85 | 4.22 | 3.99, 3.84, 4.22, 2.85, 2.91 |
| cold path, bench suite: first test result | 5 | 5.71 | 6.40 | 4.42 | 6.62 | 5.71, 6.62, 6.07, 4.49, 4.42 |
| cold path, demo refunds PR: total | 5 | 7.28 | 8.71 | 6.80 | 9.20 | 9.20, 7.98, 6.93, 7.28, 6.80 |
| cold path, demo refunds PR: first failure reported | 5 | 6.40 | 7.77 | 5.92 | 8.28 | 8.28, 7.02, 6.05, 6.40, 5.92 |
| cold path, demo refunds PR: sandbox + install | 5 | 3.22 | 4.04 | - | - | in that run's bench-raw.json |
| cold path, demo refunds PR: first test result | 5 | 4.59 | 5.81 | - | - | in that run's bench-raw.json |
| webhook -> first test (pooled sandbox, demo rerun) | 5 | 5.54 | 6.41 | 4.10 | 6.44 | 4.10, 6.37, 5.54, 4.60, 6.44 |
| webhook -> sandbox ready | 5 | 1.51 | 2.73 | 1.11 | 2.90 | 1.45, 2.90, 1.51, 1.11, 2.47 |
| webhook -> the failing test's first result | 5 | 5.98 | 6.57 | 4.10 | 6.71 | 4.10, 6.37, 5.98, 4.96, 6.71 |
| webhook -> first confirmed failure (incl. its retry) = verdict | 5 | 7.33 | 7.99 | 4.92 | 8.23 | 4.92, 7.33, 7.62, 6.21, 8.23 |
| webhook run's file-order estimate, from the first test (`t_first_failure_file_order_est`) | 5 | 7.90 | 8.88 | 6.40 | 8.90 | 6.40, 8.84, 7.90, 6.96, 8.90 |

## Fan-out runs

| shards | run | status | shards obtained | last test s | total s | runs.cost_estimate | cost (sandbox lifetime, limits) |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | `run_01M4NKXXF1Z7D6S5TQ18Y45GRK` | completed success | 1 | 182.8 | 184.1 | $0.0169 | $0.0169 |
| 1 | `run_01M4NM3HA1JZ87AA6RJ3C9XHM7` | completed success | 1 | 181.1 | 181.9 | $0.0167 | $0.0167 |
| 1 | `run_01M4NM92Z28GVM1PE2D50FJBA4` | completed success | 1 | 179.2 | 180.0 | $0.0166 | $0.0165 |
| 1 | `run_01M4NMEJR3NNWQFSTT2QCEC0FA` | completed success | 1 | 179.4 | 180.2 | $0.0166 | $0.0166 |
| 1 | `run_01M4NMM2QSK1F3YYDMDCG4WFG6` | completed success | 1 | 192.2 | 193.1 | $0.0178 | $0.0177 |
| 16 | `run_01M4NMSZCQS5GKBS23VATYKQXT` | completed success | 16 | 22.2 | 28.3 | $0.0401 | $0.0393 |
| 16 | `run_01M4NMTV1PDAVHNGPJW4M9DRWF` | completed success | 16 | 17.1 | 22.6 | $0.0321 | $0.0315 |
| 16 | `run_01M4NMVH6ADWCQYG5RXTRBF81R` | completed success | 16 | 19.6 | 25.4 | $0.0360 | $0.0356 |
| 16 | `run_01M4NMWA1ES26VG67CNCPGNKH5` | completed success | 16 | 18.1 | 23.4 | $0.0333 | $0.0327 |
| 16 | `run_01M4NMX0YYY0X5750GHG34G8KY` | completed success | 16 | 19.6 | 25.3 | $0.0360 | $0.0355 |
| 32 | `run_01M4NMXSPY0T61BYRBFQ3F4SEP` | completed success | 32 | 13.2 | 22.7 | $0.0631 | $0.0607 |
| 32 | `run_01M4NMYFXDM562481DBV0YZEXY` | completed success | 32 | 13.7 | 25.2 | $0.0702 | $0.0675 |
| 32 | `run_01M4NMZ8GPZNY57BJ7BDR8T1A2` | completed success | 32 | 11.8 | 20.9 | $0.0580 | $0.0554 |
| 32 | `run_01M4NMZWZV2814N2WBYRV6QTPQ` | completed success | 32 | 14.8 | 25.0 | $0.0696 | $0.0669 |
| 32 | `run_01M4NN0NF7VVJPV0NW4Z32SP1Q` | completed success | 32 | 12.8 | 22.5 | $0.0624 | $0.0597 |

## Demo on Modal (seeded history and both PRs)

| run | verdict | rank coverset / file order | reused |
| --- | --- | --- | --- |
| main-1 | success | - | True |
| main-2 | success | - | True |
| main-3 | success | - | True |
| main-4 | success | - | True |
| main-5 | success | - | True |
| main-6 | success | - | True |
| pr-1 | failure | 1/66 | True |
| pr-2 | success | - | True |

## Capacity errors

None recorded.

Largest shard count that succeeded on every repetition: 32.

## Cost model

Modal bills `max(request, usage)` per second: $0.142 per core-hour, $0.024 per GiB-hour. Every
sandbox requests 0.125 core and 512 MiB with limits of `COVERSET_SHARD_CPU_LIMIT` (2.0) and
`COVERSET_SHARD_MEMORY_MIB` (2048). `cost_bench_N` prices each sandbox's whole lifetime at the
limit rate; `cost_bench_N_exec` is the recorded estimate (`runs.cost_estimate`: the time the run
held each worker, at the limit rate). Neither bounds the other: the recorded estimate is the
higher one in the 16- and 32-shard runs. A single-threaded pytest uses about one core and
far less than 2 GiB, so the bill is lower than either.

## Commands

```sh
uv run coverset modal smoke
COVERSET_DATA_DIR=<scratch> uv run coverset demo --executor modal --no-serve
uv run python scripts/bench_modal.py --max-cost-usd 2.00 --repetitions 5 --shards 1,16,32
uv run coverset modal reap --all
```

Raw per-repetition data (timelines, run ids) is kept in `<data-dir>/bench-raw.json` of the run.
A scenario that failed is measured again on its own with `--resume --data-dir <same dir>` (the
2026-10-11 run did this for `webhook`, whose first attempt hit an import error in a module that
was being edited at the time); `--docs-only` rewrites these files from the raw data.

## Webhook to verdict

The README's refunds-PR sentence and every `t_webhook_*` key come from one session of 5 trials on
2026-10-11 (the second pass that day, after the derandomized demo profile changed the demo SHAs and
the file-order estimate began to start at the first test result; these values replace the first
pass's). The demo was seeded on Modal
(`COVERSET_DATA_DIR=<scratch> uv run coverset demo --executor modal --no-serve`), then the
`webhook` scenario of `scripts/bench_modal.py` ran on its own against the same data dir
(`--scenarios webhook --repetitions 5 --no-docs --max-cost-usd 0.30`): a trusted snapshot of `main`,
the warm pool filled (`COVERSET_POOL_SIZE` 4; the serve-time pool maintainer kept it topped up, so
trials 2 and 4 found it already full), 3 s idle, then a re-signed `check_run.rerequested` delivery
for the refunds PR posted to the in-process app. Every run took all 4 of its workers from the pool.
The seeding ran with `COVERSET_MODAL_DAILY_BUDGET_USD=0.30`, whose admission reservations stopped it
after `main-3` (`budget exhausted`, nothing spent); the benchmark script, with its own budget, ran
the remaining baselines and both PRs before the trials, in the same session.

Every time is seconds from `timeline.webhook_received`, read from the `runs` table and, for the
failing test's first result, from its first `test` event with outcome `failed` in `run_events`.
"Confirmed" is `timeline.first_failure`: the retry failed too and the early verdict was posted in the
same moment (`timeline.verdict`). The file-order estimate is the run's own
`t_first_failure_file_order_est`, from the same origin and estimated from the first test: the time
to the first test result (environment, collection and planning, which file order pays too) plus
`Σ p50` of the 65 tests before the failing one in collection order. It counts no retry. In every
trial the failed result arrived before the estimate (by 1.9 s at the median), and so did the
confirmed verdict. The demo suite's tests take 3.50 s in all (`demo_suite_s`: the sum of the 97 test
durations of the `main-6` baseline on Modal), so ordering cannot save much more on this suite; the
measured cold path on the same PR (`cold_path_demo` above: a fresh sandbox, dependency install,
plain pytest in file order) reports the failure after 6.40 s from its own start.

Modal, executor `modal`, 4 shards, pooled sandboxes:

| trial | run | sandbox ready | first test | failing test's first result | confirmed = verdict | file-order estimate | rank coverset / executed / file order |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | `run_01M4NZ18BTHA5G4PXR3JVVPTHD` | 1.45 | 4.10 | 4.10 | 4.92 | 6.40 | 1 / 1 / 66 |
| 2 | `run_01M4NZ1JPR4NFV7WQ83ZTDN55W` | 2.90 | 6.37 | 6.37 | 7.33 | 8.84 | 1 / 1 / 66 |
| 3 | `run_01M4NZ206GHEJKPD7Z8HSJ760B` | 1.51 | 5.54 | 5.98 | 7.62 | 7.90 | 1 / 45 / 66 |
| 4 | `run_01M4NZ2C0CW4ATZW5NGH88RVQY` | 1.11 | 4.60 | 4.96 | 6.21 | 6.96 | 1 / 42 / 66 |
| 5 | `run_01M4NZ2Q5R4TJRCGWM1Q9TFNJD` | 2.47 | 6.44 | 6.71 | 8.23 | 8.90 | 1 / 24 / 66 |
| median | | 1.51 | 5.54 | 5.98 | 7.33 | 7.90 | |
| p90 | | 2.73 | 6.41 | 6.57 | 7.99 | 8.88 | |

Collection and planning (`first_test - sandbox_ready`, `t_webhook_collect`) took 2.64 to 4.03 s,
median 3.49 (p90 4.01). Sandbox-ready times are higher than in the first pass (0.77 s median then);
every run still claimed its 4 workers from the pool. In trials 1 and 2 the failing test was the
run's first result. `rank_executed` is how many results had arrived when the failure was confirmed:
the four shards run at once, so other shards keep reporting while the failing test's retry runs.

Local executor, for comparison (`coverset demo --executor local --no-serve`, then the same delivery
replayed five times through the in-process app with `COVERSET_LOCAL_ALLOW_WEBHOOK_RUNS=true`),
3 shards:

| trial | run | sandbox ready | first test | confirmed = verdict | file-order estimate | rank coverset / executed / file order |
| ---: | --- | ---: | ---: | ---: | ---: | --- |
| 1 | `run_01M4NYVSW5BXZ45W444GNCVMY3` | 0.17 | 0.84 | 1.07 | 2.49 | 1 / 2 / 66 |
| 2 | `run_01M4NYVZ950SR7YDQQGRTSBF5V` | 0.13 | 0.78 | 1.01 | 2.45 | 1 / 1 / 66 |
| 3 | `run_01M4NYW4NXQAX0135TS3WQSVZZ` | 0.13 | 0.76 | 0.99 | 2.40 | 1 / 1 / 66 |
| 4 | `run_01M4NYWA2VM6K9S1BYD063260Z` | 0.13 | 0.77 | 1.02 | 2.40 | 1 / 12 / 66 |
| 5 | `run_01M4NYWFFR4TSYFEJWGMJMA6M8` | 0.13 | 0.76 | 0.99 | 2.40 | 1 / 1 / 66 |
| median | | 0.13 | 0.77 | 1.01 | 2.40 | |
| p90 | | 0.16 | 0.82 | 1.05 | 2.47 | |

Cost: the Modal runs of this session recorded $0.030 of `runs.cost_estimate`, and the spend ledger
(`executor_spend`) $0.0027 more for the image build, the snapshot and the pool sandboxes' idle time;
the script's own tracker put the scenario at $0.030, against its pre-run estimate of $0.265 at the
limit rate. Afterwards `uv run coverset modal reap --all --any-instance` found 0 sandboxes.

## Instrumentation overhead

`overhead_x` in the README: the demo app's suite (97 tests) run in three modes, interleaved, in a
temporary copy, by `packages/pytest-coverset/scripts/measure_overhead.py --target examples/demo-app
--source ledger --repeats 5` on 2026-10-11 (local, Python 3.13.0, Apple silicon), re-measured after
the plugin hardening pass (tracer check at every context switch, `f_locals` handling). Each time is
the wall time of a `python -m pytest` subprocess, interpreter start-up and imports included.

| mode | median s | runs |
| --- | ---: | --- |
| plain pytest | 2.554 | 2.549, 2.554, 2.544, 2.565, 2.558 |
| per-test coverage (ctrace core, branch, one context per test) | 2.895 | 2.895, 2.876, 2.894, 2.914, 2.910 |
| per-test coverage plus argument shapes | 2.918 | 2.916, 2.918, 2.906, 2.931, 2.928 |

`overhead_x` = 2.918 / 2.554 = 1.14 (coverage alone 1.13; the first pass that day measured 1.16).
The demo suite is small and its wall time is mostly start-up, so this is a floor: suites that spend
their time in tight Python loops pay more under the ctrace core. The bench suite does not measure
overhead at all. Its `busy(ms)` bodies calibrate their loop size at import time, under whatever
tracer is active, so every test spends its drawn milliseconds with or without coverage; its fan-out
numbers measure parallelism, not instrumentation cost.

## README keys added in phase 9

Every value below is in `docs/numbers.json`; `scripts/check_readme.py` renders the README's numbers
paragraph and the root README's bullet from that file. Medians with p90 in brackets; webhook and
overhead values are from the second pass on 2026-10-11.

| key | value | source |
| --- | --- | --- |
| `demo_tests` | 97 | tests collected at the demo's `main` head (`main-6` run) |
| `pr1_tests` | 100 | tests collected at the refunds PR's head |
| `demo_runs` | 8 | runs behind the screenshots: six `main` baselines and two PRs, local executor |
| `shapes_runs` | 5 | baseline runs `coverset shapes` pools (`main-2` to `main-6`) |
| `demo_suite_s` | 3.5 | sum of the 97 test durations of the `main-6` baseline on Modal (3.499 s) |
| `rank_coverset`, `rank_file_order` | 1, 66 (n = 5) | the refunds PR's failing test in all five webhook trials |
| `t_webhook_sandbox_ready` | 1.51 (2.73) | webhook trials above |
| `t_webhook_first_test` | 5.54 (6.41) | webhook trials above |
| `t_webhook_collect` | 3.49 (4.01) | `first_test - sandbox_ready` in the webhook trials |
| `t_webhook_first_failed_result` | 5.98 (6.57) | the failing test's first `test` event |
| `t_webhook_first_failure` | 7.33 (7.99) | confirmed after the retry (`timeline.first_failure`) |
| `t_verdict` | 7.33 (7.99) | `timeline.verdict`; `t_verdict_executor` is `modal`, `t_verdict_origin` is `webhook_received` |
| `t_first_failure_file_order_est` (alias `t_verdict_file_order_est`) | 7.9 (8.88) | the runs' own estimate, from the first test |
| `t_verdict_local`, `t_verdict_file_order_est_local` | 1.01 (1.05), 2.4 (2.47) | local trials above |
| `overhead_x` | 1.14 | `overhead_full_s` 2.918 / `overhead_plain_s` 2.554 (`overhead_cov_s` 2.895), `overhead_repeats` 5, `overhead_target` `examples/demo-app` |
| `missing_classes`, `expected_classes` | 16, 34 | the three `coverset shapes --markdown` tables in the README (2 of 10, 7 of 11, 7 of 13 missing) |
| `screenshot_date`, `t_verdict_date`, `t_webhook_date`, `overhead_date` | 2026-10-11 | |

## Real repository (anonymised): 470 files, 5,099 tests

The control plane's own numbers on a real repository: one `pytest --coverset` run of a private
Python service (5,099 tests collected, 5,081 results; 464 measured source files; 5,343 function
regions; `bundle.tar.gz` 53 MB, `coverage.sqlite` 123 MB holding 3.18 million arcs), ingested into a
scratch control plane as two default-branch baselines and one pull-request run (the same bundle,
diffed against a commit 30 commits back: 128 changed files), then served in-process through the
Quart test client. File names are left out on purpose; "the largest file" is a 2,205-line module.

Measured on 2026-10-13 on an Apple M-series machine (16 CPUs, macOS 15, Python 3.13.0, DuckDB
1.5.6, pyarrow 26.0.0) that eleven other agents were using at the same time: load averages ran
from 8 to 40. To keep that out of the comparison, **before** and **after** were run interleaved,
three times each, on the same scratch control plane, and each cell is the lowest of the three (the
run least disturbed). Before is commit `0f3f382` (`git archive HEAD` on `PYTHONPATH`), after is
the working tree with this change; both read the same files. Server time is from request to the
last body byte, without gzip unless the row says so.

### Pages

Twenty requests per page after one cold request (the first in a fresh process).

| Page | before cold | before p50 | before p95 | after cold | after p50 | after p95 |
|---|---:|---:|---:|---:|---:|---:|
| dashboard | 70 | 35 | 37 | 75 | 13 | 14 |
| repo overview | 220 | 217 | 244 | 118 | 35 | 38 |
| map (gap) | 117 | 109 | 126 | 76 | 70 | 98 |
| map (lines) | 110 | 109 | 132 | 69 | 71 | 97 |
| map (inputs) | 104 | 108 | 127 | 71 | 73 | 98 |
| source, largest file | 858 | 312 | 343 | 322 | 21 | 23 |
| line-tests fragment (busiest line) | 42 | 36 | 71 | 11 | 7 | 8 |
| tests page | 328 | 320 | 352 | 347 | 339 | 355 |
| test page | 1,227 | 1,154 | 1,222 | 297 | 21 | 22 |
| function page (in the largest file) | 199 | 192 | 240 | 126 | 98 | 139 |
| pull request page | 1,769 | 351 | 375 | 946 | 60 | 104 |

All values in milliseconds. The target was every page under 150 ms at p95 and the largest file's
source view under 100 ms: every page meets it except the tests page (below). Bytes and gzip, from
the end-to-end run (`after2`). The gzip requests send `Sec-Fetch-Site: same-origin`, as a browser
does on the app's own navigation. The app gzips a dynamic page only for such requests, because of
BREACH (docs/data-model.md, "Caches").

| Page | HTML | gzipped (level 3) | p50 with gzip |
|---|---:|---:|---:|
| map (gap) | 2.98 MB | 267 KB | 81 ms |
| tests page | 3.45 MB | 200 KB | 345 ms |
| source, largest file | 1.04 MB | 110 KB | 24 ms |

The facts SQL path (`FactsStore.query`, one `SELECT ... FROM file_cov ... LIMIT 20` scoped to a
run, as `coverset facts query` and the agent API run it) went from 11.1 ms p50 (11.9 p95) to 1.4 ms
(1.7 p95) once its scope's connection is cached; `line_tests` from 11.7 ms (14.9) to 4.5 ms (4.9).
These two come from the end-to-end runs (`before`, `after2`), not the interleaved ones.

Where the time went, and what replaced it:

| Hot spot (before) | Pages | Fix |
|---|---|---|
| `RunFacts.sampling` read `func_sampling` from Parquet once per function: 200 reads per page | test, source, PR | rows indexed by `func_key` once per run (`web/loaders.py` `IndexedRunFacts`) |
| one `git cat-file` process per source file (22 on a test page, 17 ms each) | test, source, function | blob texts cached by commit; the first miss reads every measured source of the run in one `git cat-file --batch` |
| `np.searchsorted` of Python ints over the 2.1M-row `test_lines` arrays converted the whole array per call | every facts page | each file's rows found through an index built once |
| function views, line facts and body coverage rebuilt per request | source, PR, test | memoised per run (a run's facts never change under its stamp) |
| a fresh DuckDB database with views per query (5 to 10 ms each, five or more per page) | overview, function, test, dashboard | one cached database per scope, leased as cursors (`FactsStore.lease`) |
| the treemap, gaps, class changes and function history scanned `test_lines` per request | overview, map, function | per-run summaries written at finalize (`func_body`, `tree_leaf`, `line_hits`) and memoised results |
| `call_shapes` turned into 89,000 Python objects to answer one function | PR (cold), source (cold) | built per function from row numbers |
| the line popover loaded all 5,081 results of the run to show 40 | line-tests fragment | only the listed tests' results (`loaders.result_rows`) |
| Pygments lexed the 2,205-line file on every render | source | the process's `SourceCache` (`loaders.source_cache`) |

Still over, or close to, the target, all outside this change's files (open issues in the report):

* **Tests page, 340 ms**: Jinja renders `tests_row.html` through an `{% include %}` and the
  nodeid macro 5,081 times (3.4 MB of HTML); the database part is about 30 ms of it and no facts
  are read. Paginating (or rendering the rows in one template loop without the include) is the fix.
* **Function page, 98 ms p50, 139 ms p95**: `web/detail.py` renders the function's hunk with
  `render_source` without the shared `SourceCache`, so the whole 2,205-line file is lexed per
  request. Passing `cache=source_cache()` there was measured at 39 ms p50 and 69 ms p95.
* **Map, 70 ms p50**: about 40 % of it is `url_for`, called once per tile (5,400 tiles) by the
  closures in `repos.map_page`; building each URL prefix once per page would remove most of that.

### Ingest of the real bundle

One run, in a fresh process each, interleaved before and after twice (the lower of the two shown):

| Phase | before s | after s |
|---|---:|---:|
| extract `bundle.tar.gz` | 0.25 | 0.22 |
| convert (`read_any_bundle`, local limits) | 3.78 | 2.96 |
| index the head sources (5,343 regions) | 0.64 | 0.64 |
| `ingest_shard` | 1.38 | 0.65 |
| `finalize_run` without the test-economy pass | 0.51 | 0.59 |
| `finalize_run` with it (it did not exist before) | | 1.66 |
| total | 6.73 | 5.21 (6.27 with the economy pass) |
| peak RSS of the process | 608 MB | 674 MB (863 MB with the economy pass) |

`finalize_run` now also writes the three summaries (0.06 s). Convert is bound by SQLite's
`group_concat` over the arc table (1.1 s of the 3 s) and by parsing 98,000 lines of
`shapes.jsonl.gz` (0.5 s).

### Parquet per run

| Table | rows | bytes |
|---|---:|---:|
| `test_lines` | 2,077,269 | 2,215,284 |
| `call_shapes` | 89,113 | 652,327 |
| `test_economy` | 5,081 | 256,990 |
| `functions` | 5,343 | 241,409 |
| `func_sampling` | 3,284 | 164,430 |
| `tests_dim` | 5,082 | 142,207 |
| `func_body` (new summary) | 3,571 | 125,170 |
| `func_testing` | 3,502 | 86,770 |
| `func_input_cov` | 4,100 | 86,631 |
| `line_hits` (new summary) | 42,027 | 78,053 |
| `file_static` | 464 | 73,474 |
| `tree_leaf` (new summary) | 4,053 | 47,692 |
| `file_arcs_possible` | 11,704 | 41,801 |
| `run_arcs` | 8,216 | 31,307 |
| `test_families` | 498 | 24,006 |
| `shapes` | 1,211 | 20,040 |
| `run_economy` | 1 | 14,701 |
| `file_cov` | 464 | 13,493 |
| `files` | 715 | 9,892 |
| `ingest_log` | 1 | 4,878 |
| `runs_dim` | 1 | 3,118 |
| whole run | | 4,333,673 |

About 2,300 such runs fit in 10 GB of facts (docs/data-model.md, "Measured ceiling").

### Memory

| Process | before | after |
|---|---:|---:|
| server after 21 requests of every page above (no ingest in the process) | 1,343 to 1,358 MB | 867 to 880 MB |
| the bench process after three in-process ingests and every page | 1,683 MB | 1,458 MB |

Those two rows touch only three runs. A server that has served many runs is bounded by the web's
run-facts cache (`web/loaders.py`): at most `COVERSET_RUN_FACTS_ENTRIES` (8) runs and
`COVERSET_RUN_FACTS_MB` (384) of *estimated* bytes. `IndexedRunFacts.memory_bytes` weighs
everything an entry holds: the `test_lines` arrays (25 MB on this bundle), the sorted `call_shapes`
Arrow table (16 MB) and the Python graphs of statics, regions, rows, the repo dictionary and the
memoised views. It sizes containers by sampling. A fully warmed run of this bundle (line facts and
a popover on every file, the shapes, sampling and input-class view of 600 functions) weighs 86 MB.
An exact object walk gives 77 MB for the same run, and the sampled estimate takes 7.5 ms once per
run. The budget is checked on every lookup, not only on insertion: an entry loads lazily, so it
weighs almost nothing when it is inserted.

`--cache-memory 16` clones a run 16 times under new run ids and warms each through the cache. The
process starts at 172 MB RSS and 122 MB footprint, and Arrow's live memory stays at the cached
runs' tables throughout. After the 16th run:

| `COVERSET_RUN_FACTS_MB` | runs kept | estimated | live Arrow | footprint (macOS) | RSS (`ps`) |
|---:|---:|---:|---:|---:|---:|
| 128 | 1 | 86 MB | 40 MB | 624 MB | 937 MB |
| 256 | 2 | 173 MB | 79 MB | 714 MB | 1,040 MB |
| 384 (default) | 4 | 346 MB | 159 MB | 782 MB | 1,224 MB |
| 768 | 8 (the entry bound) | 691 MB | 318 MB | 1,071 MB | 1,570 MB |

The footprint (`proc_pid_rusage`, what Activity Monitor shows) is the number to size by on macOS.
`ps` RSS also counts pages that malloc freed but has not yet returned (`MADV_FREE`). A loop that
reads and sorts one `test_lines` 24 times and keeps nothing still climbs from 189 to 504 MB RSS,
while its footprint stays between 109 and 151 MB.

Above the cached runs themselves, there is a floor of about 450 MB. It is reached within about
eight runs and grows slowly after that. It is fragmentation, not a leak: `tracemalloc` shows the
Python heap flat at the cached runs' size from the fifth run on, and `pa.total_allocated_bytes()`
is flat too. Releasing Arrow's pool and calling `malloc_zone_pressure_relief` after each eviction
made no measurable difference, so neither is done.

The earlier estimate counted only the per-line arrays: 32 MB for a warmed run against 120 to
180 MB of RSS. It let 15 runs stay cached at about 1.8 GB RSS. Linux was not measured: glibc
returns freed pages differently, so measure there with the same command before sizing a
container.

Sizing for a 5,000-test repository on one server process:

| Machine | Settings |
|---|---|
| 2 GB or more | the defaults |
| 1 to 2 GB | `COVERSET_RUN_FACTS_MB=128`, `COVERSET_RUN_FACTS_ENTRIES=4`, `COVERSET_FACTS_CACHE_ENTRIES=8`, `COVERSET_FACTS_THREADS=2` |

The run-facts budget comes on top of:

* up to `COVERSET_FACTS_CACHE_ENTRIES` (16) DuckDB databases, each limited to
  `COVERSET_FACTS_MEMORY_LIMIT` (512MB) while it runs a query;
* the source-text cache (`COVERSET_SOURCE_CACHE_MB`, 64);
* the allocator keeping freed query memory. The Python heap stays at 13 MB across repeated ingests
  (`tracemalloc`), while RSS settles about 600 MB higher after the first DuckDB aggregations.

The test-economy pass of `finalize_run` adds 200 MB per finalize on this bundle and 2 GB on the
synthetic run below.

### Commands

```sh
# a scratch control plane from a `pytest --coverset` output (repo read only; its mirror is a clone)
uv run python scripts/bench_facts.py --real <repo>/.coverset --repo-root <repo> \
    --data-dir <scratch>/real --pr-base $(git -C <repo> rev-parse HEAD~30) --json-out <scratch>/real.json
# the pages of the same control plane again, without ingesting
uv run python scripts/bench_facts.py --real <repo>/.coverset --repo-root <repo> \
    --data-dir <scratch>/real --pr-base <same sha> --pages-only
# memory while 16 distinct runs pass through the run-facts cache (clones of the first run, deleted
# afterwards); set COVERSET_RUN_FACTS_MB / COVERSET_RUN_FACTS_ENTRIES to compare budgets
uv run python scripts/bench_facts.py --real <repo>/.coverset --repo-root <repo> \
    --data-dir <scratch>/real --cache-memory 16
```

The before column ran the same page loop with `PYTHONPATH=<git archive HEAD>/coverset/src`.

## Synthetic facts store: 10,000 tests × 2,000 files

`uv run python scripts/bench_facts.py` at its defaults (2,000 files, 200,000 statement lines,
10,000 tests × 2,000 lines, 8 shards, 20,160,000 `test_lines` rows per run, 30 runs), on the same
loaded machine. One run's eight pytest-coverset archives (`bundle.tar.gz`, 323 MB together) through
the control plane's own path in a fresh process, strict limits, before and after:

| Phase (8 shards) | before s | after s |
|---|---:|---:|
| extract | 2.46 | 2.64 |
| read (`read_any_bundle`) | 18.74 | 18.89 |
| `ingest_shard` | 65.10 | 6.33 |
| `finalize_run` without / with the test-economy pass | 0.46 / (none) | 0.95 / 7.11 |
| total | 86.76 | 30.67 (34.97) |
| peak RSS | 994 MB | 839 MB (2,803 MB with the economy pass) |

`ingest_shard` was 10x slower than it needed to be: building `func_sampling` called
`np.searchsorted` with Python ints on the shard's 2.5-million-row line arrays once per sampled
function, converting the whole array every time, and the line arrays were assembled from one small
numpy array per (test, file) pair. Both are now array operations over the shard
(`facts/ingest.py` `_LineIndex`). The neutral-format archives (10.5 MB for the same run) read in
24 s: JSON parsing in `coverset_model.io`.

Facts queries on the newest run, through `facts/queries.py` (old code: a fresh connection per
call; new: a leased cursor, summaries and memoised results), p50 / p95 in ms:

| Query | before | after |
|---|---:|---:|
| `line_tests` | 14.7 / 15.9 | 7.6 / 8.6 |
| `patch_coverage`, 500 changed lines in 5 files | 20.9 / 22.5 | 14.7 / 17.5 |
| `treemap`, uncached | 92.2 / 97.8 | 49.7 / 54.4 |
| `treemap`, memoised | (no memo) | 0.3 / 0.3 |
| `run_summary`, uncached | 34.1 / 36.3 | 24.7 / 25.9 |
| `top_gaps`, uncached | 82.9 / 91.1 | 6.0 / 6.4 |

## Compact layout

`test_file_bits` (docs/data-model.md, "Compact layout") against `test_lines`, measured on a
finalized run of each repository with DuckDB reading the Parquet files directly:

| | real bundle | synthetic |
|---|---:|---:|
| cells | 2,077,269 | 20,160,000 |
| (test, file) rows | 115,572 | 202,000 |
| `test_lines` Parquet | 2.22 MB | 5.86 MB |
| `test_lines` sorted by test instead | 1.31 MB | 9.29 MB |
| `test_file_bits`, sorted by (test, file) | 0.33 MB | 1.24 MB |
| `test_file_bits`, sorted by (file, test) | 0.39 MB | 0.46 MB |
| build (`lines_to_bitsets`) | 0.17 s | 0.39 s per 2.5M-cell shard |
| one test's lines, p50 / p95 | 3.4 / 4.2 ms long, 1.5 / 2.0 ms bits | 6.3 / 12.7 ms long, 2.8 / 4.8 ms bits |
| one line's tests, p50 / p95 | 2.3 / 2.8 ms long, 1.6 / 2.2 ms bits | 6.3 / 8.9 ms long, 2.6 / 5.0 ms bits |
| every cell back to arrays (`bitsets_to_lines`) | 0.04 s | 0.11 s |
