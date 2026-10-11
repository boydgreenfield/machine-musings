# Benchmarks

Live Modal measurements behind the README numbers paragraph, written by `scripts/bench_modal.py`.
Medians and p90 over the listed repetitions; every value is in seconds unless marked.
The bench suite is **synthetic** (`examples/bench-suite`: 1,200 CPU-bound tests, 173.7 s of drawn
serial work); the cold path is a fresh `debian_slim` sandbox that installs dependencies and runs
plain `pytest` in file order. It is a measured stand-in for a cold CI job, not GitHub Actions.

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
| estimated spend of this benchmark | $0.695 (sandbox lifetimes at the CPU/memory limits; an upper bound) |

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
| webhook -> first test (pooled sandbox, demo rerun) | 5 | 4.14 | 4.63 | 3.04 | 4.89 | 4.89, 4.25, 3.04, 3.04, 4.14 |
| webhook -> sandbox ready | 5 | 0.94 | 1.31 | 0.67 | 1.47 | 1.47, 1.07, 0.80, 0.67, 0.94 |
| webhook -> first confirmed failure (incl. its retry) | 5 | 5.33 | 6.37 | 3.85 | 6.47 | 6.47, 6.21, 3.85, 3.92, 5.33 |

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
`COVERSET_SHARD_MEMORY_MIB` (2048). `cost_bench_N` prices each sandbox's whole lifetime at its
limits (an upper bound); `cost_bench_N_exec` is what the run records (`runs.cost_estimate`:
the time it held each worker, at the limits). A single-threaded pytest uses about one core and
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
