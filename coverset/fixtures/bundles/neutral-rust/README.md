# Rust capture and neutral bundle (real)

**Recorded, not synthetic.** `capture/` is what `coverset rust run` left behind for
[`examples/rust-app`](../../../examples/rust-app) (cargo-nextest 0.9.148, rustc 1.98.1, LLVM
source-based coverage, one process and one profile per test). Its paths were made relative to
`repo/`, `created_at` was fixed and start times were rebased onto a fixed clock. `repo/` is a copy
of the crate's sources. `bundle/` and `bundle.tar.gz` are the neutral bundle the converter makes
from the capture. The converter is pure Python, so the tests that read this fixture need no cargo.
See [`docs/rust.md`](../../../docs/rust.md).

    uv run python scripts/make_rust_fixture.py                 # rerun the crate (needs cargo-nextest
                                                               # and llvm-tools) and rewrite everything
    uv run python scripts/make_rust_fixture.py --from-capture  # rebuild bundle/ and repo/ from capture/
    uv run python scripts/make_rust_fixture.py --check         # exit 1 when bundle/ or repo/ is stale

| Path | What it is |
|---|---|
| `capture/capture.json` | format `coverset-rust-capture` v1: runner, rustc, timings, the two test binaries (`ledger`, `ledger::ledger_flow`), the 16 tests with outcomes and export paths |
| `capture/exports/tests/<key>.lcov` | the lines each test executed (`llvm-cov export -format=lcov`, executed `DA` lines only); `key` is `blake2b(binary_id \0 name, 8)` |
| `capture/exports/merged.lcov` | every instrumented line of both binaries (the statements) |
| `capture/exports/llvm-functions.json` | LLVM's function records by source position: binaries, instantiations, closures |
| `repo/` | `Cargo.toml`, `Cargo.lock`, `src/` (five modules), `tests/ledger_flow.rs` |
| `bundle/` | `manifest.json` (`language: rust`, producer `coverset-rust`), `tests.jsonl` (16), `files.jsonl` (5), `coverage.jsonl`, `functions.jsonl` (12 regions with type descriptors); no arcs, no shapes |
| `bundle.tar.gz` | the same files as a flat archive (`coverset_model.io.pack`) |

What the tests read from it (`tests/lang/`):

- 15 tests passed, `durations::tests::parses_days` is `#[ignore]`d: `skipped`, marker `ignore`, no lines.
- `ledger money::tests::refuses_zero_weights` executed exactly lines 19–22 and 38 of
  `src/money.rs`: the early return of `allocate_cents` and its closing brace.
- `ledger::ledger_flow a_failed_transfer_reports_the_shortfall` ran `Ledger::transfer`'s lines 83,
  84 and 87 but not 85–86: the first posting fails, so the second never runs. A diff over 84–86
  has patch coverage 1 of 3.
- `src/money.rs` lines 90–123 (the `#[cfg(test)]` module) are `excluded`, never statements.
- `largest<T>` is monomorphised twice (`f64` and `i32`); every one of the 12 regions matches an
  LLVM function record (`meta.llvm_functions`).
- The bundle is 14 KB, the capture 17 KB, the whole fixture 49 KB.
