"""End to end: ``examples/rust-app`` through the real pipeline (cargo, LLVM, the converter, the report).

Skipped unless a Rust toolchain is present: ``cargo``/``rustc``, the ``llvm-tools`` component
(``rustup component add llvm-tools-preview``) and, for the nextest test, ``cargo nextest``
(``cargo install --locked cargo-nextest``). The libtest runner needs only the first two. The crate is
copied to a temporary directory, so its ``target/`` and ``.coverset/`` stay out of the checkout.

When this machine's rustc is the one the committed fixture was recorded with, the per-test line
sets must equal the fixture's exactly: that is the check that ``fixtures/bundles/neutral-rust`` is
what the pipeline really produces, not an approximation of it.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from coverset.cli import cli
from coverset.lang.rust_llvm import (
    CaptureError,
    find_llvm_tools,
    nextest_version,
    run_rust,
    rustc_version,
)
from coverset_model.io import read_bundle, validate

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
CRATE = ROOT / "examples" / "rust-app"
FIXTURE = ROOT / "fixtures" / "bundles" / "neutral-rust"


def _toolchain_missing() -> str | None:
    """Why the e2e tests cannot run, or None. Runs at collection, so it must never raise: a
    ``CaptureError`` here (any toolchain probe failing) would abort the whole pytest session."""
    try:
        find_llvm_tools()
    except CaptureError as exc:  # ToolchainError included
        return f"no Rust toolchain with llvm-tools: {exc}"
    return None


MISSING = _toolchain_missing()
NEXTEST = nextest_version() if MISSING is None else None
needs_rust = pytest.mark.skipif(MISSING is not None, reason=MISSING or "")
needs_nextest = pytest.mark.skipif(
    NEXTEST is None, reason=MISSING or "cargo-nextest is not installed (cargo install --locked cargo-nextest)"
)


def _copy_crate(tmp: Path) -> Path:
    crate = tmp / "rust-app"
    crate.mkdir(parents=True)
    for name in ("Cargo.toml", "Cargo.lock", "src", "tests"):
        src = CRATE / name
        if src.is_dir():
            shutil.copytree(src, crate / name)
        else:
            shutil.copy2(src, crate / name)
    return crate


def _check_against_fixture(out: Path) -> None:
    assert validate(out) == []
    bundle = read_bundle(out)
    fixture = read_bundle(FIXTURE / "bundle")
    assert [c.nodeid for c in bundle.collected] == [c.nodeid for c in fixture.collected]
    assert [r.qualname for r in bundle.functions] == [r.qualname for r in fixture.functions]
    assert sorted(t.outcome for t in bundle.tests) == ["passed"] * 15 + ["skipped"]
    llvm = json.loads((out / "manifest.json").read_text())["meta"]["llvm_functions"]
    assert llvm["unexplained"] == [] and llvm["not_instrumented"] == []
    recorded = json.loads((FIXTURE / "capture" / "capture.json").read_text())["rustc"]
    if rustc_version() == recorded:
        assert bundle.coverage == fixture.coverage
        assert bundle.static == fixture.static
    else:  # another rustc may attribute a closing brace differently; the shape must still hold
        assert set(bundle.coverage) == set(fixture.coverage)
        assert set(bundle.static) == set(fixture.static)


@needs_rust
def test_libtest_runner(tmp_path: Path) -> None:
    crate = _copy_crate(tmp_path)
    summary = run_rust(crate, out=tmp_path / "neutral", root=crate, runner="libtest", jobs=4)
    assert summary.runner == "libtest" and summary.tests == 16
    assert summary.outcomes == {"passed": 15, "skipped": 1}
    _check_against_fixture(tmp_path / "neutral")


@needs_nextest
def test_nextest_runner_through_the_cli_and_the_report(tmp_path: Path) -> None:
    crate = _copy_crate(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["rust", "run", "--crate", str(crate), "--root", str(crate), "--runner", "nextest", "-j", "4"]
        + ["--archive", str(tmp_path / "rust.tar.gz")],
    )
    assert result.exit_code == 0, result.output
    assert "view it with: coverset report" in result.output and "15 passed" in result.output
    out = crate / ".coverset" / "neutral"
    assert (out / "files.jsonl.gz").is_file()  # compressed: what `coverset-ci upload` reads
    _check_against_fixture(out)
    assert validate(tmp_path / "rust.tar.gz") == []
    capture = json.loads((crate / ".coverset" / "rust" / "capture.json").read_text())
    assert capture["runner"] == "nextest" and capture["runner_version"] == NEXTEST
    built = runner.invoke(
        cli, ["report", str(out), "--trusted", "--repo-root", str(crate), "--out", str(tmp_path / "r")]
    )
    assert built.exit_code == 0, built.output
    pages = list((tmp_path / "r").rglob("*.html"))
    assert len(pages) >= 12 + 5 + 16
    money = (tmp_path / "r" / "files" / "src" / "money.rs.html").read_text()
    assert "Source not available." not in money and "Ledger::transfer" in money


@needs_nextest
def test_a_failing_test_keeps_its_panic(tmp_path: Path) -> None:
    crate = _copy_crate(tmp_path)
    with open(crate / "tests" / "ledger_flow.rs", "a", encoding="utf-8") as fh:
        fh.write(
            "\n#[test]\nfn deliberately_fails() {\n"
            "    assert_eq!(allocate_cents(100, &[1, 1, 1]).unwrap(), vec![33, 33, 34]);\n}\n"
        )
    by_runner = {}
    for runner in ("nextest", "libtest"):
        out = tmp_path / f"neutral-{runner}"
        summary = run_rust(crate, out=out, root=crate, runner=runner, jobs=4)  # type: ignore[arg-type]
        assert summary.outcomes == {"passed": 15, "skipped": 1, "failed": 1}
        bundle = read_bundle(out)
        failed = next(t for t in bundle.tests if t.outcome == "failed")
        assert failed.nodeid == "ledger::ledger_flow deliberately_fails"
        assert failed.longrepr is not None and "panicked at tests/ledger_flow.rs" in failed.longrepr
        by_runner[runner] = (failed.fail_sig, bundle.coverage[failed.nodeid])
    assert by_runner["nextest"] == by_runner["libtest"]  # same signature, same lines either way


GREET_MAIN = """fn shout(name: &str) -> String {
    if name.is_empty() {
        return "NOBODY".to_string();
    }
    name.to_uppercase()
}

fn main() {
    let name = std::env::args().nth(1).unwrap_or_default();
    println!("{}", shout(&name));
}

#[cfg(test)]
mod tests {
    #[test]
    fn shouts() {
        assert_eq!(super::shout("a"), "A");
    }
}
"""
GREET_CLI = """use std::process::Command;

#[test]
fn runs_the_binary() {
    let out = Command::new(env!("CARGO_BIN_EXE_greet")).arg("").output().unwrap();
    assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "NOBODY");
}
"""


@needs_rust
@pytest.mark.parametrize("runner", ["libtest", pytest.param("nextest", marks=needs_nextest)])
def test_a_cli_test_covers_the_binary_it_spawns(tmp_path: Path, runner: str) -> None:
    """A test that runs the crate's own executable gets that process's lines too (its profile is a
    second ``-%p`` file of the same test, exported against the executable)."""
    crate = tmp_path / "greet"
    (crate / "src").mkdir(parents=True)
    (crate / "tests").mkdir()
    (crate / "Cargo.toml").write_text('[package]\nname = "greet"\nversion = "0.1.0"\nedition = "2021"\n')
    (crate / "src" / "main.rs").write_text(GREET_MAIN)
    (crate / "tests" / "cli.rs").write_text(GREET_CLI)
    run_rust(crate, out=tmp_path / "neutral", root=crate, runner=runner, jobs=2)  # type: ignore[arg-type]
    assert validate(tmp_path / "neutral") == []
    coverage = read_bundle(tmp_path / "neutral").coverage
    assert coverage["greet::bin/greet tests::shouts"] == {"src/main.rs": (1, 2, 4, 5, 6)}
    spawned = coverage["greet::cli runs_the_binary"]["src/main.rs"]
    assert {3, 8, 9, 10} <= set(spawned)  # main() and the empty-name branch ran in the child


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


ROOT_PACKAGE_WORKSPACE = {
    "Cargo.toml": '[package]\nname = "rootpkg"\nversion = "0.1.0"\nedition = "2021"\n\n'
    '[dependencies]\nalpha = { path = "crates/alpha" }\n\n'
    '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\n',
    "src/lib.rs": "pub fn twice(x: i64) -> i64 {\n    alpha::double(x)\n}\n\n"
    "#[cfg(test)]\nmod tests {\n    #[test]\n    fn twice_doubles() {\n"
    "        assert_eq!(super::twice(2), 4);\n    }\n}\n",
    "crates/alpha/Cargo.toml": '[package]\nname = "alpha"\nversion = "0.1.0"\nedition = "2021"\n',
    "crates/alpha/src/lib.rs": "mod util;\n\npub fn double(x: i64) -> i64 {\n    x * 2\n}\n\n"
    "pub fn clamp(x: i64) -> i64 {\n    util::clamp_to(x, 10)\n}\n\n"
    "#[cfg(test)]\nmod tests {\n    #[test]\n    fn clamps() {\n"
    "        assert_eq!(super::clamp(20), 10);\n    }\n}\n",
    "crates/alpha/src/util.rs": "pub fn clamp_to(x: i64, top: i64) -> i64 {\n"
    "    if x > top {\n        return top;\n    }\n    x\n}\n",
    "crates/beta/Cargo.toml": '[package]\nname = "beta"\nversion = "0.1.0"\nedition = "2021"\n',
    "crates/beta/src/lib.rs": "pub fn negate(x: i64) -> i64 {\n    -x\n}\n",
    "crates/beta/tests/flow.rs": "fn setup_helper() -> i64 {\n    3\n}\n\n"
    "#[test]\nfn negates() {\n    assert_eq!(beta::negate(setup_helper()), -3);\n}\n",
}


@needs_rust
@pytest.mark.parametrize("runner", ["libtest", pytest.param("nextest", marks=needs_nextest)])
def test_a_workspace_whose_root_is_a_package_runs_every_member(tmp_path: Path, runner: str) -> None:
    """Cargo's default at such a root is the root package alone; coverset passes ``--workspace``
    so the members' tests run, and a member's ``tests/`` stays out of the measured files."""
    ws = tmp_path / "ws"
    _write(ws, ROOT_PACKAGE_WORKSPACE)
    summary = run_rust(ws, out=tmp_path / "neutral", root=ws, runner=runner, jobs=2)  # type: ignore[arg-type]
    assert summary.outcomes == {"passed": 3}
    bundle = read_bundle(tmp_path / "neutral")
    assert sorted(c.nodeid for c in bundle.collected) == [
        "alpha tests::clamps",
        "beta::flow negates",
        "rootpkg tests::twice_doubles",
    ]
    assert sorted(bundle.static) == [
        "crates/alpha/src/lib.rs",
        "crates/alpha/src/util.rs",
        "crates/beta/src/lib.rs",
        "src/lib.rs",
    ]
    assert "setup_helper" not in {r.qualname for r in bundle.functions}
    assert bundle.coverage["alpha tests::clamps"]["crates/alpha/src/util.rs"] == (1, 2, 3, 6)
    assert bundle.coverage["beta::flow negates"] == {"crates/beta/src/lib.rs": (1, 2, 3)}


@needs_nextest
def test_a_crate_path_with_a_space_runs_under_nextest(tmp_path: Path) -> None:
    """Cargo splits a target-runner value on whitespace; the runner script then lives in a
    temporary directory without any, and is gone once the run is over."""
    crate = _copy_crate(tmp_path / "with space")
    before = set(Path(tempfile.gettempdir()).glob("coverset-rust-*"))
    summary = run_rust(crate, out=tmp_path / "neutral", root=crate, runner="nextest", jobs=4)
    assert summary.outcomes == {"passed": 15, "skipped": 1}
    _check_against_fixture(tmp_path / "neutral")
    assert set(Path(tempfile.gettempdir()).glob("coverset-rust-*")) == before
