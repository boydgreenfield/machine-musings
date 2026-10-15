"""The toolchain-facing parts of the Rust pipeline that can be checked without a Rust toolchain.

Each test stands in for a failure the review of ``coverset rust run`` found on a real machine:
a rustup shim with no usable toolchain (which must read as "toolchain missing", never as a crash
at pytest collection), a workspace whose root is itself a package (whose members' tests must
run), a crate path with a space (which cargo cannot pass to a target runner) and a large suite
(whose per-test profile matching must stay linear). ``cargo`` and ``rustc`` are replaced by
fakes through :func:`coverset.lang.rust_llvm._run`; the real pipeline is in
``tests/e2e/test_rust_adapter.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from coverset.lang import rust_llvm
from coverset.lang.rust_llvm import (
    RUNNER_SCRIPT,
    CaptureError,
    ToolchainError,
    _cargo_layout,
    _list_profiles,
    _place_runner,
    _runner_profiles,
    capture_env,
    find_llvm_tools,
    host_triple,
)


def _fake_run(stdout: str = "", *, fail: bool = False) -> Any:  # noqa: ANN401 - a stand-in with _run's signature
    def run(argv: Sequence[Any], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if fail:
            raise CaptureError(f"{argv[0]} failed (exit 1):\nerror: toolchain 'nope' is not installed")
        return subprocess.CompletedProcess([str(a) for a in argv], 0, stdout, "")

    return run


def test_a_broken_rustc_shim_is_a_missing_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rustup shim exists but exits non-zero (no default toolchain, a bogus override): that is
    a :class:`ToolchainError`, which the e2e module's collection-time probe turns into a skip."""
    monkeypatch.setattr(rust_llvm, "_run", _fake_run(fail=True))
    monkeypatch.delenv("LLVM_PROFDATA", raising=False)
    monkeypatch.delenv("LLVM_COV", raising=False)
    with pytest.raises(ToolchainError, match="no usable Rust toolchain"):
        find_llvm_tools()
    with pytest.raises(ToolchainError):
        host_triple()


def test_a_bogus_rustup_toolchain_is_a_missing_toolchain(tmp_path: Path) -> None:
    """The reviewer's repro, for real: whether or not rustc is installed here, a toolchain name
    that cannot exist ends in ToolchainError (never the base CaptureError)."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "RUSTUP_TOOLCHAIN": "coverset-nonexistent-toolchain",
        "RUSTUP_AUTO_INSTALL": "0",
    }
    with pytest.raises(ToolchainError):
        find_llvm_tools(env)


def _metadata(workspace_root: Path, members: dict[str, Path]) -> str:
    return json.dumps(
        {
            "workspace_root": str(workspace_root),
            "packages": [
                {
                    "id": f"path+file://{root}#{name}@0.1.0",
                    "manifest_path": str(root / "Cargo.toml"),
                    "targets": [{"name": name, "kind": ["lib"], "src_path": str(root / "src" / "lib.rs")}],
                }
                for name, root in members.items()
            ],
        }
    )


def test_the_workspace_root_runs_every_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the workspace root (a root package plus members), cargo's default would be the root
    package alone; coverset passes ``--workspace``. At a member, cargo's own choice stands."""
    ws = tmp_path / "ws"
    members = {"rootpkg": ws, "alpha": ws / "crates" / "alpha", "beta": ws / "crates" / "beta"}
    monkeypatch.setattr(rust_llvm, "_run", _fake_run(_metadata(ws, members)))
    at_root = _cargo_layout(ws, {})
    assert at_root.scope == ("--workspace",)
    assert at_root.targets[(f"path+file://{ws / 'crates' / 'beta'}#beta@0.1.0", "beta")] == (
        ws / "crates" / "beta" / "src" / "lib.rs",
        ws / "crates" / "beta",
    )
    assert _cargo_layout(ws / "crates" / "alpha", {}).scope == ()


def test_the_libtest_and_nextest_stages_pass_the_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both stage-1 drivers put the layout's scope on every cargo command that selects packages."""
    seen: list[list[str]] = []

    def run(argv: Sequence[Any], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append([str(a) for a in argv])
        out = '{"rust-suites": {}, "rust-build-meta": {}}' if "list" in seen[-1] else ""
        return subprocess.CompletedProcess(seen[-1], 0, out, "")

    monkeypatch.setattr(rust_llvm, "_run", run)
    layout = rust_llvm.CargoLayout({}, ("--workspace",))
    work = tmp_path / "work"
    rust_llvm._prepare_work(work)
    rust_llvm._run_libtest(tmp_path, work, 1, {}, lambda _m: None, layout)
    assert seen[0][:3] == ["cargo", "test", "--workspace"]
    seen.clear()
    with pytest.raises(CaptureError, match="no JUnit report"):  # the fake ran nothing
        rust_llvm._run_nextest(tmp_path, work, 1, {}, lambda _m: None, layout)
    assert [argv[:4] for argv in seen] == [
        ["cargo", "nextest", "list", "--message-format"],
        ["cargo", "nextest", "run", "--workspace"],
    ]
    assert "--workspace" in seen[0]


def test_a_crate_path_with_a_space_gets_a_runner_cargo_can_exec(tmp_path: Path) -> None:
    work = tmp_path / "with space" / ".coverset" / "rust"
    work.mkdir(parents=True)
    script, temp = _place_runner(work)
    try:
        assert temp is not None and script.parent == temp
        assert not any(ch.isspace() for ch in str(script))
        assert script.read_text() == RUNNER_SCRIPT and script.stat().st_mode & 0o111
        env = capture_env(work, "aarch64-apple-darwin", {}, runner=script)
        assert env["CARGO_TARGET_AARCH64_APPLE_DARWIN_RUNNER"] == str(script)
        assert env["COVERSET_PROFILE_DIR"] == str(work / "profiles" / "tests")  # quoted in the script
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)  # what run_rust does once stage 1 is over
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _place_runner(plain) == (plain / "runner.sh", None)


def test_profiles_are_matched_in_one_listing(tmp_path: Path) -> None:
    """Grouping by the stem before the last ``-``: retries keep the last attempt, spawned processes
    (several ``-<pid>`` files) stay with their test, stray files are ignored."""
    d = tmp_path / "tests"
    d.mkdir()

    def idfile(name: str, binary: str, test: str, attempt: int, pids: Sequence[int]) -> None:
        (d / name).write_text(f"{binary}\n{test}\n{attempt}\n")
        for pid in pids:
            (d / f"{name}-{pid}.profraw").write_bytes(b"")

    idfile("t.aaaa", "ledger", "money::tests::a", 1, [11])
    idfile("t.bbbb", "ledger", "money::tests::flaky", 1, [12])
    idfile("t.cccc", "ledger", "money::tests::flaky", 2, [13])
    idfile("t.dddd", "greet::cli", "runs", 1, [20, 21])
    idfile("t.eeee", "ledger", "never_wrote_a_profile", 1, [])
    (d / "stray.txt").write_text("x")
    found = _runner_profiles(d)
    assert found[("ledger", "money::tests::a")] == [d / "t.aaaa-11.profraw"]
    assert found[("ledger", "money::tests::flaky")] == [d / "t.cccc-13.profraw"]
    assert found[("greet::cli", "runs")] == [d / "t.dddd-20.profraw", d / "t.dddd-21.profraw"]
    assert ("ledger", "never_wrote_a_profile") not in found
    assert _list_profiles(tmp_path / "missing") == ({}, [])


def test_profile_matching_is_linear(tmp_path: Path) -> None:
    """6,000 tests took 56 s with one glob per test; one listing keeps it well under a second."""
    d = tmp_path / "tests"
    d.mkdir()
    n = 6000
    for i in range(n):
        (d / f"t.{i:010d}").write_text(f"bin\ntest_{i}\n1\n")
        (d / f"t.{i:010d}-{1000 + i}.profraw").write_bytes(b"")
    clock = time.monotonic()
    found = _runner_profiles(d)
    elapsed = time.monotonic() - clock
    assert len(found) == n and all(len(paths) == 1 for paths in found.values())
    assert elapsed < 5, f"matching {n} tests took {elapsed:.2f} s"
