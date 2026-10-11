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
