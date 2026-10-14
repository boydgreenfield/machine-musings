"""Per-test Rust coverage from LLVM source-based coverage, written as a neutral bundle.

The pipeline has three stages, and the boundary between the second and the third is a directory
on disk (the *capture*), so the last stage is pure Python and runs without a Rust toolchain (the
committed fixture under ``fixtures/bundles/neutral-rust`` is a capture):

1. **Run** every test in its own process with a profile file of its own. With cargo-nextest
   (the default when it is installed) the suite runs once, as nextest normally runs it (one
   process per test, ``-j`` in parallel), with two additions passed through the environment and a
   tool config file, so the crate is never modified: ``RUSTFLAGS=-C instrument-coverage``, and a
   *target runner* (``CARGO_TARGET_<TRIPLE>_RUNNER``, a five-line ``sh`` script) that nextest puts
   in front of every test process. nextest sets ``NEXTEST_BINARY_ID``, ``NEXTEST_TEST_NAME`` and
   ``NEXTEST_ATTEMPT`` in that process's environment; the runner creates a unique file with
   ``mktemp``, writes those three values into it and sets ``LLVM_PROFILE_FILE`` to the same name
   plus ``-%p.profraw`` before ``exec``-ing the test, so every profile is named for exactly one
   test attempt. (``LLVM_PROFILE_FILE`` itself only expands ``%p``, ``%h``, ``%m``, ``%c`` and
   ``%t``, none of which is the test; ``%p`` here keeps the profiles of processes the test spawns,
   such as a CLI test running the crate's own binary, apart from the test's own, and they are
   merged into the test's export.) Outcomes, durations and panic messages come from nextest's JUnit
   report, which the tool config file turns on in a ``coverset`` profile stored under the work
   directory. Without nextest (``--runner libtest``), coverset does the same thing itself: ``cargo
   test --no-run`` builds the test binaries, ``<binary> --list`` lists them, and each test runs as
   ``<binary> --exact NAME --test-threads 1`` in a thread pool of ``jobs`` workers.
2. **Export** each test's profile: ``llvm-profdata merge -sparse`` turns the ``.profraw`` files
   into an indexed profile and ``llvm-cov export -format=lcov`` against that test's own binary
   (plus the crate's non-test executables when the test spawned processes) gives the lines it
   executed (LLVM's own line algorithm, so a line counts exactly when ``llvm-cov show`` would show
   it as hit). One merged profile over every test gives the measurable lines (every
   ``DA`` line of the merged export) and, from the JSON export, LLVM's function records (name,
   span, instantiation count), kept as a small digest. Exports run ``jobs`` at a time.
3. **Convert** (:func:`convert_capture`): the capture's tests, the per-test line sets, the merged
   statements and the Rust sources (indexed by :mod:`coverset.lang.rust`) become a
   :class:`~coverset_model.neutral.NeutralBundle`: tests named ``<binary-id> <test name>`` (what
   nextest prints, so an id can be pasted into ``-E 'test(=...)'``), test-only code (``#[cfg(test)]``
   modules, ``#[test]`` functions) excluded from the statements, function regions and declared
   types from the sources, no arcs (stable rustc has no branch coverage; see docs/rust.md), no
   shapes.

Measured files are the sources under a package root outside ``tests/``, ``benches/``,
``examples/`` and ``build.rs``. Paths in the bundle are relative to the repository root (the git
work tree's top by default), so an uploaded bundle matches the head commit's tree.

Cost (the trade-off docs/rust.md discusses): one process per test (what nextest does anyway), one
``llvm-profdata`` and one ``llvm-cov`` process per test, each reading the test binary's coverage
mapping. On a 300-test crate, run serially, a test costs about 12 ms to run and 28 ms to export;
both stages parallelise close to linearly over ``jobs``, and the export grows with binary size.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from coverset_model.io import pack, write_bundle
from coverset_model.neutral import (
    CollectedItem,
    FrozenMap,
    FunctionRegion,
    NeutralBundle,
    StaticFile,
    TestRow,
)
from coverset_model.types import TypeDescriptor

__all__ = [
    "CAPTURE_FILE",
    "Capture",
    "CaptureError",
    "LlvmTools",
    "RunSummary",
    "TestCase",
    "ToolchainError",
    "capture_env",
    "convert_capture",
    "find_llvm_tools",
    "nextest_version",
    "parse_junit",
    "parse_lcov",
    "profile_key",
    "run_rust",
    "write_neutral",
]

CAPTURE_FILE: Final = "capture.json"
CAPTURE_FORMAT: Final = "coverset-rust-capture"
CAPTURE_VERSION: Final = 1
PRODUCER: Final = "coverset-rust"
NEXTEST_PROFILE: Final = "coverset"
NOT_MEASURED_DIRS: Final = frozenset({"tests", "benches", "examples", "target"})
MAX_MESSAGE_CHARS: Final = 8192
OUTCOMES: Final = frozenset({"passed", "failed", "error", "skipped", "xfailed", "xpassed"})
IGNORE_FILENAME_REGEX: Final = r"(/\.cargo/(registry|git)/|/\.rustup/|^/rustc/|/rustc/[0-9a-f]{40}/)"
"""Passed to ``llvm-cov``: the standard library and dependencies never reach the exports."""

RUNNER_SCRIPT: Final = """#!/bin/sh
# coverset: one LLVM profile per nextest test attempt (see coverset/lang/rust_llvm.py).
if [ -n "${NEXTEST_TEST_NAME:-}" ]; then
  base=$(mktemp "$COVERSET_PROFILE_DIR/t.XXXXXXXXXX") || exit 70
  printf '%s\\n%s\\n%s\\n' "$NEXTEST_BINARY_ID" "$NEXTEST_TEST_NAME" "${NEXTEST_ATTEMPT:-1}" > "$base"
  LLVM_PROFILE_FILE="$base-%p.profraw"; export LLVM_PROFILE_FILE
fi
exec "$@"
"""

type Runner = Literal["nextest", "libtest"]


class CaptureError(RuntimeError):
    """A capture could not be made or read (the message says what to fix)."""


class ToolchainError(CaptureError):
    """A tool the pipeline needs (cargo, llvm-tools, cargo-nextest) is missing or broken."""


# -- tools ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LlvmTools:
    """The two LLVM tools of the rustup ``llvm-tools`` component (same LLVM as rustc's)."""

    profdata: Path
    cov: Path


def _tool_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``env`` (default: this process's) with ``~/.cargo/bin`` on ``PATH`` when it exists."""
    merged = dict(os.environ if env is None else env)
    cargo_bin = Path.home() / ".cargo" / "bin"
    if cargo_bin.is_dir() and str(cargo_bin) not in merged.get("PATH", "").split(os.pathsep):
        merged["PATH"] = os.pathsep.join(p for p in (merged.get("PATH", ""), str(cargo_bin)) if p)
    return merged


def _run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = 3600,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a tool and capture its output; a missing tool is a :class:`ToolchainError`, a non-zero
    exit (with ``check``) or a timeout a :class:`CaptureError` carrying the end of its stderr."""
    try:
        result = subprocess.run(
            [str(a) for a in argv],
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolchainError(f"{argv[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"{' '.join(map(str, argv[:3]))} timed out after {timeout} s") from exc
    if check and result.returncode != 0:
        tail = (result.stderr or result.stdout)[-2000:]
        raise CaptureError(f"{' '.join(map(str, argv[:3]))} failed (exit {result.returncode}):\n{tail}")
    return result


def _rustc(args: Sequence[str], env: Mapping[str, str]) -> str:
    """``rustc ARGS``'s stdout. Any failure is a :class:`ToolchainError`, not only a missing
    ``rustc``: the rustup shim exists but fails when there is no default toolchain or the
    ``RUSTUP_TOOLCHAIN``/override names one that is not installed, and callers (the e2e probe,
    the adapter) treat "no usable toolchain" as one condition to skip or report."""
    try:
        return _run(["rustc", *args], env=env, timeout=120).stdout
    except ToolchainError:
        raise
    except CaptureError as exc:
        raise ToolchainError(f"no usable Rust toolchain: {exc}") from exc


def host_triple(env: Mapping[str, str] | None = None) -> str:
    """The host target triple (``aarch64-apple-darwin``), from ``rustc -vV``."""
    out = _rustc(["-vV"], _tool_env(env))
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise ToolchainError("rustc -vV printed no host triple")


def rustc_version(env: Mapping[str, str] | None = None) -> str:
    """``rustc 1.98.1 (48a229cea 2026-09-01)``'s version number."""
    out = _rustc(["--version"], _tool_env(env)).split()
    return out[1] if len(out) > 1 else "unknown"


def find_llvm_tools(env: Mapping[str, str] | None = None) -> LlvmTools:
    """``llvm-profdata`` and ``llvm-cov``: ``LLVM_PROFDATA``/``LLVM_COV`` when set (the names
    cargo-llvm-cov reads), else the rustup ``llvm-tools`` component of the active toolchain."""
    env = _tool_env(env)
    given = env.get("LLVM_PROFDATA"), env.get("LLVM_COV")
    if all(given):
        return LlvmTools(Path(given[0]), Path(given[1]))  # type: ignore[arg-type]
    sysroot = _rustc(["--print", "sysroot"], env).strip()
    bin_dir = Path(sysroot) / "lib" / "rustlib" / host_triple(env) / "bin"
    suffix = ".exe" if sys.platform == "win32" else ""
    tools = LlvmTools(bin_dir / f"llvm-profdata{suffix}", bin_dir / f"llvm-cov{suffix}")
    if not (tools.profdata.is_file() and tools.cov.is_file()):
        raise ToolchainError(
            f"llvm-profdata and llvm-cov are not in {bin_dir}: run `rustup component add "
            "llvm-tools-preview` (or set LLVM_PROFDATA and LLVM_COV)"
        )
    return tools


def nextest_version(env: Mapping[str, str] | None = None) -> str | None:
    """cargo-nextest's version (``0.9.148``), or None when ``cargo nextest`` is not installed."""
    try:
        result = _run(["cargo", "nextest", "--version"], env=_tool_env(env), timeout=60, check=False)
    except CaptureError:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"cargo-nextest (\S+)", result.stdout)
    return match.group(1) if match else None


# -- the capture ------------------------------------------------------------------------------------


def profile_key(binary_id: str, name: str) -> str:
    """The 16-hex-digit file key of one test: ``blake2b(binary_id \\0 name, 8)``."""
    return hashlib.blake2b(f"{binary_id}\0{name}".encode(), digest_size=8).hexdigest()


@dataclass(frozen=True, slots=True)
class Binary:
    """One test binary: nextest's binary id, the target's kind and root file, its package root."""

    binary_id: str
    kind: str
    src_path: Path
    package_root: Path
    path: Path
    """The built executable (needed for the export stage only, never recorded in the capture)."""


@dataclass(slots=True)
class TestCase:
    """One test and what its run left behind."""

    __test__ = False  # not a pytest test class

    binary_id: str
    name: str
    ignored: bool = False
    outcome: str | None = None
    duration_s: float | None = None
    message: str | None = None
    fail_sig: str | None = None
    start: float | None = None
    stop: float | None = None
    profiles: list[Path] = field(default_factory=list)
    """The test's ``.profraw`` files: its own process and every process it spawned."""
    export: str | None = None

    @property
    def test_id(self) -> str:
        """``<binary-id> <test name>``: how nextest prints it."""
        return f"{self.binary_id} {self.name}"

    @property
    def key(self) -> str:
        return profile_key(self.binary_id, self.name)


@dataclass(slots=True)
class Capture:
    """What stage 1 and 2 produced (``capture.json`` plus the ``exports/`` directory)."""

    directory: Path
    root: Path
    runner: str
    runner_version: str | None
    rustc: str | None
    binaries: list[Binary]
    tests: list[TestCase]
    created_at: str
    jobs: int
    timings_s: dict[str, float] = field(default_factory=dict)


def _runner_var(triple: str) -> str:
    """Cargo's target-runner variable for ``triple``: ``CARGO_TARGET_AARCH64_APPLE_DARWIN_RUNNER``."""
    return f"CARGO_TARGET_{triple.upper().replace('-', '_').replace('.', '_')}_RUNNER"


def capture_env(
    work: Path, triple: str, base: Mapping[str, str] | None = None, *, runner: Path | None = None
) -> dict[str, str]:
    """The environment of an instrumented, per-test-profiled nextest run (see the module docstring).

    ``RUSTFLAGS`` gets ``-C instrument-coverage`` appended (a ``build.rustflags`` in the crate's
    ``.cargo/config.toml`` is overridden by any ``RUSTFLAGS``, which is cargo's rule, not ours);
    builds go to ``target/coverset`` under the crate so the normal build cache is not invalidated;
    every process that is not a test (build scripts, ``--list``) profiles into ``profiles/other``.
    A target runner the crate configures for the host is replaced for this run. ``runner`` is the
    runner script's path (default ``work/runner.sh``); it must contain no whitespace, because cargo
    and nextest split a ``CARGO_TARGET_<TRIPLE>_RUNNER`` value into words (see :func:`_place_runner`).
    """
    env = _tool_env(base)
    flags = env.get("RUSTFLAGS", "").strip()
    env["RUSTFLAGS"] = f"{flags} -C instrument-coverage".strip()
    env["LLVM_PROFILE_FILE"] = str(work / "profiles" / "other" / "%p-%m.profraw")
    env["COVERSET_PROFILE_DIR"] = str(work / "profiles" / "tests")
    env[_runner_var(triple)] = str(runner or work / "runner.sh")
    env["CARGO_TERM_COLOR"] = "never"
    env.pop("CARGO_ENCODED_RUSTFLAGS", None)
    return env


def _nextest_tool_config(work: Path) -> str:
    store = json.dumps(str(work / "nextest-store"))
    return (
        f"# coverset: written by `coverset rust run`; the crate's own nextest config still applies.\n"
        f"[store]\ndir = {store}\n\n"
        f"[profile.{NEXTEST_PROFILE}]\nfail-fast = false\n\n"
        f'[profile.{NEXTEST_PROFILE}.junit]\npath = "junit.xml"\n'
        "store-success-output = false\nstore-failure-output = true\n"
    )


def _place_runner(work: Path) -> tuple[Path, Path | None]:
    """Write the runner script and return (its path, a temporary directory to remove afterwards).

    Cargo reads ``CARGO_TARGET_<TRIPLE>_RUNNER`` as a whitespace-separated command line with no
    quoting, so a crate under ``/Users/me/My Projects/app`` would make nextest exec
    ``/Users/me/My``. The script then goes into a fresh ``tempfile.mkdtemp`` directory instead
    (the profiles it writes stay under ``work``: the script quotes ``$COVERSET_PROFILE_DIR``); if
    even the temporary directory has whitespace, the run stops with a message that names the way
    out (``--runner libtest`` sets no target runner).
    """
    temp: Path | None = None
    where = work
    if any(ch.isspace() for ch in str(work)):
        temp = Path(tempfile.mkdtemp(prefix="coverset-rust-"))
        where = temp
        if any(ch.isspace() for ch in str(temp)):
            shutil.rmtree(temp, ignore_errors=True)
            raise CaptureError(
                f"{work} contains whitespace, which cargo cannot pass to a target runner, and so "
                f"does the temporary directory: set TMPDIR to a path without spaces, or pass "
                f"--runner libtest"
            )
    runner = where / "runner.sh"
    runner.write_text(RUNNER_SCRIPT, encoding="utf-8")
    runner.chmod(0o755)
    return runner, temp


def _prepare_work(work: Path) -> None:
    for sub in ("profiles", "exports", "nextest-store"):
        shutil.rmtree(work / sub, ignore_errors=True)
    for sub in ("profiles/tests", "profiles/other", "exports/tests"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    (work / "nextest.toml").write_text(_nextest_tool_config(work), encoding="utf-8")


type Targets = dict[tuple[str, str], tuple[Path, Path]]


@dataclass(frozen=True, slots=True)
class CargoLayout:
    """What ``cargo metadata --no-deps`` says about the crate directory, read once before the build.

    ``scope`` is the package selection passed to ``cargo nextest list/run`` and ``cargo test``:
    ``--workspace`` when the crate directory is the workspace root. Without it cargo builds only
    the *default members*, and when the root is itself a package (ripgrep, clap, nushell) that is
    the root package alone: every member's tests would silently not run while the member sources
    linked into the root's binary would still be measured, so the coverage would look plausible
    and be wrong. ``--crate`` pointing at a member keeps cargo's own choice (that member).
    """

    targets: Targets
    """``(package id, normalised target name)`` → (root source file, package root), so a binary
    id maps back to the file its tests are written in."""
    scope: tuple[str, ...]


def _cargo_layout(crate: Path, env: Mapping[str, str]) -> CargoLayout:
    meta = json.loads(
        _run(["cargo", "metadata", "--format-version", "1", "--no-deps"], cwd=crate, env=env).stdout
    )
    targets: Targets = {}
    for package in meta.get("packages", []):
        root = Path(package["manifest_path"]).parent
        for target in package.get("targets", []):
            targets[(package["id"], target["name"].replace("-", "_"))] = (Path(target["src_path"]), root)
    workspace_root = meta.get("workspace_root")
    at_root = bool(workspace_root) and Path(workspace_root).resolve() == crate.resolve()
    return CargoLayout(targets, ("--workspace",) if at_root else ())


def _run_nextest(
    crate: Path,
    work: Path,
    jobs: int,
    env: Mapping[str, str],
    log: Callable[[str], None],
    layout: CargoLayout,
) -> tuple[list[Binary], list[TestCase], dict[str, float], list[Path]]:
    """Stage 1 with nextest: list (which builds), run once, read JUnit and the runner's id files."""
    tool = [
        *layout.scope,
        "--profile",
        NEXTEST_PROFILE,
        "--tool-config-file",
        f"coverset:{work / 'nextest.toml'}",
    ]
    timings: dict[str, float] = {}
    started = time.monotonic()
    log("building the instrumented test binaries and listing tests (cargo nextest list)")
    listed = json.loads(
        _run(
            ["cargo", "nextest", "list", "--message-format", "json", *tool], cwd=crate, env=env, timeout=None
        ).stdout
    )
    timings["build_and_list"] = round(time.monotonic() - started, 3)
    targets = layout.targets
    binaries: list[Binary] = []
    tests: list[TestCase] = []
    for suite in listed.get("rust-suites", {}).values():
        key = (suite["package-id"], suite["binary-name"].replace("-", "_"))
        src_path, package_root = targets.get(
            key, (Path(suite.get("cwd", crate)), Path(suite.get("cwd", crate)))
        )
        binaries.append(
            Binary(suite["binary-id"], suite["kind"], src_path, package_root, Path(suite["binary-path"]))
        )
        for name, case in sorted(suite.get("testcases", {}).items()):
            if case.get("kind", "test") != "test":
                continue  # benchmarks are listed by newer nextests; they are not tests
            tests.append(TestCase(suite["binary-id"], name, ignored=bool(case.get("ignored"))))
    started = time.monotonic()
    log(f"running {sum(not t.ignored for t in tests)} tests, one process each (cargo nextest run -j {jobs})")
    _run(
        ["cargo", "nextest", "run", *tool, "--no-fail-fast", "--test-threads", str(jobs)],
        cwd=crate,
        env=env,
        timeout=None,  # the crate's own nextest config holds the per-test timeouts
        check=False,  # failing tests exit non-zero; the JUnit report says which
    )
    timings["run"] = round(time.monotonic() - started, 3)
    junit = work / "nextest-store" / NEXTEST_PROFILE / "junit.xml"
    if not junit.is_file():
        raise CaptureError(f"nextest wrote no JUnit report at {junit}: did the build fail?")
    results = parse_junit(junit.read_text(encoding="utf-8"))
    profiles = _runner_profiles(work / "profiles" / "tests")
    for test in tests:
        found = results.get((test.binary_id, test.name))
        if found is not None:
            test.outcome, test.duration_s, test.message, test.fail_sig, test.start = found
            test.stop = (
                None if test.start is None or test.duration_s is None else test.start + test.duration_s
            )
        elif test.ignored:
            test.outcome = "skipped"
        test.profiles = profiles.get((test.binary_id, test.name), [])
    meta = listed.get("rust-build-meta", {})
    target_dir = Path(meta.get("target-directory", crate / "target"))
    executables = [
        target_dir / entry["path"]
        for entries in meta.get("non-test-binaries", {}).values()
        for entry in entries
        if entry.get("kind") == "bin-exe" and entry.get("path")
    ]
    return binaries, tests, timings, executables


def _list_profiles(directory: Path) -> tuple[dict[str, list[Path]], list[Path]]:
    """One listing of a profiles directory: ``.profraw`` files grouped by their stem before the
    last ``-`` (``<prefix>-<pid>.profraw``, the prefix being a runner id file's name or a libtest
    test's key), and every other regular file (the runner's id files).

    One ``scandir`` and a dictionary, never a glob per test: globbing once per test re-reads the
    whole directory (2N entries) N times, which was 56 s of matching for 6,000 tests.
    """
    profiles: dict[str, list[Path]] = {}
    others: list[Path] = []
    with contextlib.suppress(FileNotFoundError), os.scandir(directory) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            if entry.name.endswith(".profraw"):
                prefix, sep, _pid = entry.name.removesuffix(".profraw").rpartition("-")
                if sep:
                    profiles.setdefault(prefix, []).append(Path(entry.path))
            else:
                others.append(Path(entry.path))
    for paths in profiles.values():
        paths.sort()
    return profiles, others


def _runner_profiles(directory: Path) -> dict[tuple[str, str], list[Path]]:
    """The runner script's id files: ``(binary id, test name)`` → the last attempt's profiles."""
    profiles, idfiles = _list_profiles(directory)
    best: dict[tuple[str, str], tuple[int, list[Path]]] = {}
    for idfile in idfiles:
        if not idfile.name.startswith("t."):
            continue
        lines = idfile.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) < 2:
            continue
        attempt = int(lines[2]) if len(lines) > 2 and lines[2].isdigit() else 1
        found = profiles.get(idfile.name, [])
        key = (lines[0], lines[1])
        if found and attempt >= best.get(key, (0, found))[0]:
            best[key] = (attempt, found)
    return {key: paths for key, (_, paths) in best.items()}


_PANIC_AT: Final = re.compile(r"panicked at ([^\s:][^:\n]*):(\d+)(?::\d+)?")
_THREAD_ID: Final = re.compile(r"(thread '[^'\n]*') \(\d+\)")


def _failure(message: str | None, test_id: str) -> tuple[str | None, str]:
    """The cleaned message (thread ids removed, so it is stable) and the failure signature: a hash
    of the panic location (``panicked at src/x.rs:12``), never of the message."""
    text = _THREAD_ID.sub(r"\1", message or "").strip() or None
    match = _PANIC_AT.search(text or "")
    where = f"panic\0{match.group(1)}:{match.group(2)}" if match else f"failure\0{test_id}"
    if text and len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS]
    return text, hashlib.blake2b(where.encode(), digest_size=8).hexdigest()


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    with contextlib.suppress(ValueError):
        return round(datetime.fromisoformat(value).timestamp(), 6)
    return None


def parse_junit(
    text: str,
) -> dict[tuple[str, str], tuple[str, float | None, str | None, str | None, float | None]]:
    """nextest's JUnit report → ``(binary id, test name)`` → (outcome, duration, message,
    fail_sig, start). A ``<failure>`` or ``<error>`` is ``failed`` (a test that passed on a retry
    has neither, only ``<flakyFailure>``), ``<skipped>`` is ``skipped``."""
    out: dict[tuple[str, str], tuple[str, float | None, str | None, str | None, float | None]] = {}
    root = ET.fromstring(text)
    for suite in root.iter("testsuite"):
        binary_id = suite.get("name", "")
        for case in suite.iter("testcase"):
            name = case.get("name", "")
            try:
                duration: float | None = round(float(case.get("time", "")), 6)
            except ValueError:
                duration = None
            start = _timestamp(case.get("timestamp"))
            failure = case.find("failure")
            if failure is None:
                failure = case.find("error")
            if failure is not None:
                raw = (failure.text or failure.get("message") or "").strip()
                message, sig = _failure(raw, f"{binary_id} {name}")
                out[(binary_id, name)] = ("failed", duration, message, sig, start)
            elif case.find("skipped") is not None:
                out[(binary_id, name)] = ("skipped", duration, None, None, start)
            else:
                out[(binary_id, name)] = ("passed", duration, None, None, start)
    return out


def _binary_id(package: str, kind: str, name: str) -> str:
    """nextest's binary id for a target: ``pkg`` for a library, ``pkg::name`` for an integration
    test, ``pkg::bin/name`` for a binary (``example/``, ``bench/`` likewise)."""
    if kind in ("lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"):
        return package
    if kind == "test":
        return f"{package}::{name}"
    return f"{package}::{kind}/{name}"


def _run_libtest(
    crate: Path,
    work: Path,
    jobs: int,
    env: Mapping[str, str],
    log: Callable[[str], None],
    layout: CargoLayout,
) -> tuple[list[Binary], list[TestCase], dict[str, float], list[Path]]:
    """Stage 1 without nextest: build with ``cargo test --no-run``, list and run each test binary
    one test per process (what nextest does), ``jobs`` at a time."""
    timings: dict[str, float] = {}
    started = time.monotonic()
    log("building the instrumented test binaries (cargo test --no-run)")
    built = _run(
        ["cargo", "test", *layout.scope, "--no-run", "--message-format", "json"],
        cwd=crate,
        env=env,
        timeout=None,
    )
    targets = layout.targets
    binaries: list[Binary] = []
    executables: list[Path] = []
    for line in built.stdout.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("reason") != "compiler-artifact" or not msg.get("executable"):
            continue
        if not (msg.get("profile") or {}).get("test"):
            executables.append(Path(msg["executable"]))  # a `bin` a CLI test may spawn
            continue
        target = msg["target"]
        kind = target["kind"][0]
        package = _package_name(msg.get("package_id", ""))
        src_path = Path(target["src_path"])
        package_root = targets.get(
            (msg.get("package_id", ""), target["name"].replace("-", "_")), (src_path, crate)
        )[1]
        binaries.append(
            Binary(
                _binary_id(package, kind, target["name"]),
                kind,
                src_path,
                package_root,
                Path(msg["executable"]),
            )
        )
    tests: list[TestCase] = []
    by_id = {b.binary_id: b for b in binaries}
    for binary in binaries:
        listing = _run([binary.path, "--list", "--format", "terse"], cwd=binary.package_root, env=env).stdout
        ignored_listing = _run(
            [binary.path, "--list", "--format", "terse", "--ignored"], cwd=binary.package_root, env=env
        ).stdout
        ignored = {ln.removesuffix(": test") for ln in ignored_listing.splitlines() if ln.endswith(": test")}
        for ln in sorted(listing.splitlines()):
            if ln.endswith(": test"):
                name = ln.removesuffix(": test")
                tests.append(TestCase(binary.binary_id, name, ignored=name in ignored))
    timings["build_and_list"] = round(time.monotonic() - started, 3)
    runnable = [t for t in tests if not t.ignored]
    log(f"running {len(runnable)} tests, one process each ({jobs} at a time)")
    started = time.monotonic()

    def run_one(test: TestCase) -> None:
        binary = by_id[test.binary_id]
        child = {
            **env,
            "LLVM_PROFILE_FILE": str(work / "profiles" / "tests" / f"{test.key}-%p.profraw"),
            "CARGO_MANIFEST_DIR": str(binary.package_root),
            "RUST_BACKTRACE": env.get("RUST_BACKTRACE", "0"),
        }
        test.start = round(time.time(), 6)
        clock = time.monotonic()
        try:
            result = subprocess.run(
                [str(binary.path), "--exact", test.name, "--test-threads", "1"],
                cwd=binary.package_root,
                env=child,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=600,
                check=False,
            )
            output, code = result.stdout + result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            output, code = "test timed out after 600 s", -1
        test.duration_s = round(time.monotonic() - clock, 6)
        test.stop = round(test.start + test.duration_s, 6)
        if code == 0 and "running 1 test" in output:
            test.outcome = "passed"
        elif code == 0:
            test.outcome = None  # the name matched nothing: never ran
        else:
            test.outcome = "failed"
            test.message, test.fail_sig = _failure(_libtest_failure(output, test.name), test.test_id)

    with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        list(pool.map(run_one, runnable))
    profiles, _ = _list_profiles(work / "profiles" / "tests")  # once, after every test ran
    for test in tests:
        test.profiles = profiles.get(test.key, [])
        if test.ignored:
            test.outcome = "skipped"
    timings["run"] = round(time.monotonic() - started, 3)
    return binaries, tests, timings, executables


def _package_name(package_id: str) -> str:
    """A package's name from its id (``path+file:///x/rust-app#ledger@0.1.0`` or the older
    ``ledger 0.1.0 (path+file:///...)``)."""
    if "#" in package_id:
        tail = package_id.rsplit("#", 1)[1]
        return tail.split("@", 1)[0] if "@" in tail else PurePosixPath(package_id.split("#")[0]).name
    return package_id.split(" ", 1)[0]


def _libtest_failure(output: str, name: str) -> str:
    """The ``---- NAME stdout ----`` section of libtest's output (the panic), else the tail."""
    marker = f"---- {name} stdout ----"
    if marker in output:
        section = output.split(marker, 1)[1]
        return section.split("\n\nfailures:", 1)[0].strip()
    return output.strip()[-MAX_MESSAGE_CHARS:]


# -- export -----------------------------------------------------------------------------------------


def parse_lcov(text: str) -> dict[str, dict[int, int]]:
    """``SF``/``DA`` records of an LCOV export → file → line → execution count."""
    out: dict[str, dict[int, int]] = {}
    current: dict[int, int] | None = None
    for line in text.splitlines():
        if line.startswith("SF:"):
            current = out.setdefault(line[3:], {})
        elif line.startswith("DA:") and current is not None:
            parts = line[3:].split(",")
            with contextlib.suppress(ValueError, IndexError):
                current[int(parts[0])] = current.get(int(parts[0]), 0) + int(parts[1])
        elif line == "end_of_record":
            current = None
    return out


def _closure(name: str) -> bool:
    """A closure's or async block's LLVM function record (v0 ``_RNC`` prefix, legacy ``{{closure}}``)."""
    return name.startswith("_RNC") or "{{closure}}" in name or "{closure#" in name


def _function_digest(exports: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """LLVM's function records grouped by source position: one row per (file, line, column).

    ``exports`` holds one JSON export per test binary, so the two ways a function gets several
    records are told apart: ``binaries`` counts the test binaries it was compiled into (a library
    function is in the library's unit-test binary and in every integration test binary), and
    ``instances`` is the most records any one binary has for it, which is above 1 only for a
    generic function monomorphised for several types. ``count`` sums the executions."""
    grouped: dict[tuple[str, int, int], dict[str, Any]] = {}
    for export in exports:
        local: dict[tuple[str, int, int], int] = {}
        for fn in (f for data in export.get("data", []) for f in data.get("functions", [])):
            regions = [r for r in fn.get("regions", []) if len(r) >= 8 and r[5] == 0 and r[7] == 0]
            if not regions or not fn.get("filenames"):
                continue
            first = min(regions, key=lambda r: (r[0], r[1]))
            end = max(r[2] for r in regions)
            key = (fn["filenames"][0], first[0], first[1])
            row = grouped.setdefault(
                key,
                {
                    "file": key[0],
                    "line": key[1],
                    "col": key[2],
                    "end_line": end,
                    "closure": _closure(fn.get("name", "")),
                    "count": 0,
                    "binaries": 0,
                    "instances": 0,
                },
            )
            row["count"] += int(fn.get("count", 0))
            row["end_line"] = max(row["end_line"], end)
            local[key] = local.get(key, 0) + 1
        for key, n in local.items():
            grouped[key]["binaries"] += 1
            grouped[key]["instances"] = max(grouped[key]["instances"], n)
    return [grouped[key] for key in sorted(grouped)]


def _objects(paths: Sequence[Path]) -> list[str | Path]:
    """``llvm-cov``'s object arguments: the first positionally, every other after ``-object``."""
    out: list[str | Path] = []
    for i, path in enumerate(dict.fromkeys(paths)):
        out += [path] if i == 0 else ["-object", path]
    return out


def _export(
    tools: LlvmTools,
    binaries: Sequence[Binary],
    tests: Sequence[TestCase],
    work: Path,
    jobs: int,
    log: Callable[[str], None],
    executables: Sequence[Path] = (),
) -> dict[str, float]:
    """Stage 2: per-test LCOV exports, the merged LCOV export and the function digest.

    A test with one profile is exported against its own test binary only; a test with several
    (it spawned processes, typically the crate's own ``bin``) also against ``executables``, the
    crate's non-test executables, whose coverage mappings those child profiles need.
    """
    timings: dict[str, float] = {}
    by_id = {b.binary_id: b for b in binaries}
    exports = work / "exports"
    executables = [e for e in executables if e.is_file()]
    profiled = [t for t in tests if t.profiles and t.binary_id in by_id]
    log(f"exporting {len(profiled)} per-test profiles (llvm-profdata + llvm-cov, {jobs} at a time)")
    started = time.monotonic()

    def export_one(test: TestCase) -> None:
        indexed = work / "profiles" / "tests" / f"{test.key}.profdata"
        _run([tools.profdata, "merge", "-sparse", *test.profiles, "-o", indexed])
        objects = [by_id[test.binary_id].path, *(executables if len(test.profiles) > 1 else ())]
        lcov = _run(
            [
                tools.cov,
                "export",
                "-format=lcov",
                "-skip-functions",
                f"-instr-profile={indexed}",
                f"--ignore-filename-regex={IGNORE_FILENAME_REGEX}",
                *_objects(objects),
            ]
        ).stdout
        rel = f"exports/tests/{test.key}.lcov"
        (work / rel).write_text(_hits_only(lcov), encoding="utf-8")
        test.export = rel

    with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        list(pool.map(export_one, profiled))
    timings["export_tests"] = round(time.monotonic() - started, 3)
    started = time.monotonic()
    every_object = [*(b.path for b in binaries), *executables]
    if profiled and every_object:
        listing = work / "profiles" / "inputs.txt"
        listing.write_text("".join(f"{p}\n" for t in profiled for p in t.profiles), encoding="utf-8")
        merged = work / "profiles" / "merged.profdata"
        _run([tools.profdata, "merge", "-sparse", f"--input-files={listing}", "-o", merged])
        flags = [f"-instr-profile={merged}", f"--ignore-filename-regex={IGNORE_FILENAME_REGEX}"]
        lcov = _run([tools.cov, "export", "-format=lcov", "-skip-functions", *flags, *_objects(every_object)])
        (exports / "merged.lcov").write_text(lcov.stdout, encoding="utf-8")
        per_object = [
            json.loads(_run([tools.cov, "export", "-format=text", "-skip-expansions", *flags, path]).stdout)
            for path in dict.fromkeys(every_object)
        ]
        digest = _function_digest(per_object)
        (exports / "llvm-functions.json").write_text(
            json.dumps({"functions": digest}, indent=0, sort_keys=True) + "\n", encoding="utf-8"
        )
    timings["export_merged"] = round(time.monotonic() - started, 3)
    return timings


def _hits_only(lcov: str) -> str:
    """An LCOV export without its zero-count ``DA`` lines and summary records (a test's export
    lists every instrumented line of the binary; only the executed ones matter, and dropping the
    rest makes the stored export about ten times smaller)."""
    keep = []
    for line in lcov.splitlines():
        if line.startswith("DA:"):
            if not line.endswith(",0"):
                keep.append(line)
        elif line.startswith(("SF:", "end_of_record")):
            keep.append(line)
    return "\n".join(keep) + "\n"


def _write_capture(capture: Capture) -> Path:
    def rel(path: Path) -> str:
        with contextlib.suppress(ValueError):
            return PurePosixPath(*path.resolve().relative_to(capture.root.resolve()).parts).as_posix() or "."
        return str(path)

    doc = {
        "format": CAPTURE_FORMAT,
        "version": CAPTURE_VERSION,
        "root": str(capture.root.resolve()),
        "runner": capture.runner,
        "runner_version": capture.runner_version,
        "rustc": capture.rustc,
        "created_at": capture.created_at,
        "jobs": capture.jobs,
        "timings_s": capture.timings_s,
        "binaries": [
            {
                "binary_id": b.binary_id,
                "kind": b.kind,
                "src_path": rel(b.src_path),
                "package_root": rel(b.package_root),
            }
            for b in sorted(capture.binaries, key=lambda b: b.binary_id)
        ],
        "tests": [
            {
                "binary_id": t.binary_id,
                "name": t.name,
                "ignored": t.ignored,
                "outcome": t.outcome,
                "duration_s": t.duration_s,
                "message": t.message,
                "fail_sig": t.fail_sig,
                "start": t.start,
                "stop": t.stop,
                "export": t.export,
            }
            for t in sorted(capture.tests, key=lambda t: (t.binary_id, t.name))
        ],
        "merged": "exports/merged.lcov",
        "functions": "exports/llvm-functions.json",
    }
    path = capture.directory / CAPTURE_FILE
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# -- convert ----------------------------------------------------------------------------------------


def _load_capture(directory: Path, root: Path | None) -> tuple[dict[str, Any], Path]:
    path = directory / CAPTURE_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaptureError(f"cannot read {path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("format") != CAPTURE_FORMAT:
        raise CaptureError(f"{path} is not a coverset Rust capture")
    if doc.get("version") != CAPTURE_VERSION:
        raise CaptureError(
            f"{path} has capture version {doc.get('version')!r}; this coverset reads {CAPTURE_VERSION}"
        )
    resolved = root if root is not None else (directory / str(doc.get("root", "."))).resolve()
    return doc, resolved


def _inside(directory: Path, rel: str) -> Path:
    """``directory / rel`` when ``rel`` is a plain relative path that stays inside ``directory``."""
    target = (directory / rel).resolve()
    if not target.is_relative_to(directory.resolve()):
        raise CaptureError(f"{rel!r} leaves the capture directory")
    return target


def _rel_to_root(name: str, root: Path) -> str | None:
    path = Path(name)
    if not path.is_absolute():
        parts = PurePosixPath(name).parts
        return None if ".." in parts or not parts else PurePosixPath(*parts).as_posix()
    with contextlib.suppress(ValueError, OSError):
        return PurePosixPath(*path.resolve().relative_to(root.resolve()).parts).as_posix()
    return None


def _measured(rel: str, package_roots: Sequence[str]) -> bool:
    """A source under some package root, outside ``tests/``, ``benches/``, ``examples/``,
    ``target/`` and ``build.rs`` of the package it belongs to.

    The package a file belongs to is the *most specific* root that contains it: in a workspace
    whose root is itself a package (root ``.``, members under ``crates/``), the root's empty
    prefix contains every file, so ``crates/beta/tests/flow.rs`` must be judged by
    ``crates/beta`` (an integration test: not measured), never by ``.`` (where its first
    component, ``crates``, looks like product code).
    """
    if not rel.endswith(".rs"):
        return False
    owner: str | None = None
    for package in package_roots:
        prefix = "" if package in ("", ".") else package.rstrip("/") + "/"
        if rel.startswith(prefix) and (owner is None or len(prefix) > len(owner)):
            owner = prefix
    if owner is None:
        return False
    inner = PurePosixPath(rel[len(owner) :]).parts
    return bool(inner) and inner[0] not in NOT_MEASURED_DIRS and inner != ("build.rs",)


def convert_capture(directory: Path, *, root: Path | None = None) -> NeutralBundle:
    """Stage 3: a capture directory (and the sources under ``root``) → a neutral bundle.

    ``root`` is the repository root the bundle's paths are relative to; by default the capture's
    own ``root`` (resolved against ``directory`` when relative, as in the committed fixture).
    Pure Python: no cargo, no LLVM. A capture that is not well formed raises :class:`CaptureError`.
    """
    doc, resolved = _load_capture(directory, root)
    try:
        return _convert(doc, directory, resolved)
    except CaptureError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        raise CaptureError(f"malformed capture in {directory}: {type(exc).__name__}: {exc}") from exc


def _convert(doc: Mapping[str, Any], directory: Path, root: Path) -> NeutralBundle:
    from coverset.lang.rust import CrateIndex, FileIndex, descriptors, index_file, regions

    binaries = [b for b in doc.get("binaries", []) if isinstance(b, dict)]
    package_roots = sorted({str(b.get("package_root", ".")) for b in binaries}) or ["."]
    merged_path = _inside(directory, str(doc.get("merged", "exports/merged.lcov")))
    merged = parse_lcov(merged_path.read_text(encoding="utf-8")) if merged_path.is_file() else {}
    crates: dict[str, CrateIndex] = {}
    files: dict[str, FileIndex] = {}
    for binary in binaries:
        crate = CrateIndex.build(root, root / str(binary.get("src_path", "")))
        crates[str(binary.get("binary_id"))] = crate
        for rel, found in crate.files.items():
            files.setdefault(rel, found)
    lines_by_file: dict[str, set[int]] = {}
    for name, counts in merged.items():
        rel = _rel_to_root(name, root)
        if rel is not None and _measured(rel, package_roots):
            lines_by_file.setdefault(rel, set()).update(counts)
    static: dict[str, StaticFile] = {}
    for rel in sorted(lines_by_file):
        found = files.get(rel) or index_file(root / rel)
        if found is None:
            continue
        files[rel] = found
        excluded = found.excluded_lines()
        n_lines = max(found.n_lines, 1)
        statements = tuple(sorted(n for n in lines_by_file[rel] - excluded if 1 <= n <= n_lines))
        hidden = tuple(sorted(n for n in lines_by_file[rel] & excluded if 1 <= n <= n_lines))
        static[rel] = StaticFile(rel, n_lines, statements, hidden, ())
    collected: list[CollectedItem] = []
    results: list[TestRow] = []
    durations: dict[str, float] = {}
    coverage: dict[str, dict[str, tuple[int, ...]]] = {}
    for row in sorted(doc.get("tests", []), key=lambda r: (str(r.get("binary_id")), str(r.get("name")))):
        binary_id, name = str(row.get("binary_id")), str(row.get("name"))
        test_id = f"{binary_id} {name}"
        crate = crates.get(binary_id)
        where = crate.find(name) if crate is not None else None
        if where is None:
            src = next((str(b.get("src_path")) for b in binaries if b.get("binary_id") == binary_id), "")
            where = (src or "Cargo.toml", None)  # type: ignore[assignment]
        assert where is not None
        markers = ("ignore",) if row.get("ignored") else ()
        collected.append(CollectedItem(test_id, where[0], where[1], markers, (), False))
        outcome = row.get("outcome")
        if outcome not in OUTCOMES:
            outcome = None
        if outcome is not None:
            duration = float(row.get("duration_s") or 0.0)
            failed = outcome == "failed"
            results.append(
                TestRow(
                    test_id,
                    where[0],
                    outcome,
                    duration,
                    "call" if failed else None,
                    row.get("fail_sig") if failed else None,
                    row.get("message") if failed else None,
                    row.get("start"),
                    row.get("stop"),
                )
            )
            durations[test_id] = duration
        export = row.get("export")
        if export:
            hits = parse_lcov(_inside(directory, str(export)).read_text(encoding="utf-8"))
            per_file: dict[str, tuple[int, ...]] = {}
            for fname, counts in hits.items():
                rel = _rel_to_root(fname, root)
                if rel is None or rel not in static:
                    continue
                statements = set(static[rel].statements)
                executed = tuple(sorted(n for n, c in counts.items() if c > 0 and n in statements))
                if executed:
                    per_file[rel] = executed
            if per_file:
                coverage[test_id] = per_file
    enums = {name: members for found in files.values() for name, members in found.enums.items()}
    functions: list[FunctionRegion] = []
    types: dict[tuple[str, str, int], FrozenMap[str, TypeDescriptor | None]] = {}
    for rel in sorted(static):
        found = files[rel]
        by_name = {(fn.qualname, fn.def_line): fn for fn in found.fns if not fn.is_test}
        for region in regions(rel, found):
            functions.append(region)
            fn = by_name[(region.qualname, region.def_line)]
            types[region.triple] = FrozenMap(descriptors(fn, enums))
    meta = _bundle_meta(doc, directory, root, files, static, functions)
    return NeutralBundle(
        meta=meta,
        collected=tuple(collected),
        tests=tuple(results),
        coverage=coverage,
        static=static,
        run_arcs={},
        shapes=(),
        sampling=(),
        durations=durations,
        functions=tuple(functions),
        types=FrozenMap(types),
    )


def _bundle_meta(
    doc: Mapping[str, Any],
    directory: Path,
    root: Path,
    files: Mapping[str, Any],
    static: Mapping[str, StaticFile],
    functions: Sequence[FunctionRegion],
) -> dict[str, Any]:
    """The manifest's ``meta``: how the run was made, and how LLVM's function records line up with
    the indexed regions (instantiation counts, records no region explains)."""
    meta: dict[str, Any] = {
        "runner": doc.get("runner"),
        "runner_version": doc.get("runner_version"),
        "rustc": doc.get("rustc"),
        "coverage": "llvm source-based (-C instrument-coverage), one process and one profile per test",
        "arcs": "none: stable rustc records no branch coverage, and per-test arcs are not built (v1)",
        "shapes": "none: the #[coverset::trace] shape tracer is designed, not built (docs/rust.md)",
        "jobs": doc.get("jobs"),
        "timings_s": doc.get("timings_s") or {},
    }
    digest_path = _inside(directory, str(doc.get("functions", "exports/llvm-functions.json")))
    if not digest_path.is_file():
        return meta
    digest = json.loads(digest_path.read_text(encoding="utf-8")).get("functions", [])
    by_line: dict[tuple[str, int], FunctionRegion] = {}
    for rel in static:
        found = files[rel]
        regions_by_def = {(r.qualname, r.def_line): r for r in functions if r.path == rel}
        for fn in found.fns:
            region = regions_by_def.get((fn.qualname, fn.def_line))
            if region is not None:
                by_line[(rel, fn.sig_line)] = region
    instrumented: set[str] = set()
    multi: dict[str, int] = {}
    binaries_of: dict[str, int] = {}
    unexplained: list[str] = []
    for row in digest:
        rel = _rel_to_root(str(row.get("file", "")), root)
        if rel is None or rel not in static or row.get("closure"):
            continue
        region = by_line.get((rel, int(row.get("line", 0))))
        if region is None:
            if int(row.get("line", 0)) not in static[rel].excluded:
                unexplained.append(f"{rel}:{row.get('line')}")
            continue
        instrumented.add(region.func_key)
        if int(row.get("instances", 1)) > 1:
            multi[f"{rel}::{region.qualname}"] = int(row["instances"])
        binaries_of[f"{rel}::{region.qualname}"] = int(row.get("binaries", 1))
    meta["llvm_functions"] = {
        "regions": len(functions),
        "instrumented": len(instrumented),
        "not_instrumented": sorted(
            f"{r.path}::{r.qualname}" for r in functions if r.func_key not in instrumented
        )[:50],
        "monomorphised": dict(sorted(multi.items())),
        "in_binaries": dict(sorted(binaries_of.items())),
        "unexplained": sorted(unexplained)[:50],
    }
    return meta


def write_neutral(
    bundle: NeutralBundle, out: Path, *, created_at: str | None = None, compress: bool = False
) -> Path:
    """Write ``bundle`` as a neutral bundle directory with ``language: rust``; ``compress`` writes
    ``.jsonl.gz`` rows (what ``coverset-ci upload --with-sources`` reads the files table from)."""
    from coverset import __version__

    return write_bundle(
        bundle,
        out,
        manifest={
            "language": "rust",
            "producer": {"name": PRODUCER, "version": __version__},
            "created_at": created_at,
            "shard": None,
            "meta": bundle.meta,
        },
        compress=compress,
    )


# -- the whole pipeline -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunSummary:
    """What :func:`run_rust` did: where it wrote, how many tests ended how, and how long it took."""

    out: Path
    capture: Path
    root: Path
    runner: str
    tests: int
    outcomes: dict[str, int]
    timings_s: dict[str, float]
    archive: Path | None = None


def repo_root_of(path: Path, env: Mapping[str, str] | None = None) -> Path:
    """The git work tree's top above ``path``, else ``path`` itself."""
    try:
        result = _run(["git", "rev-parse", "--show-toplevel"], cwd=path, env=env, timeout=30, check=False)
    except CaptureError:
        return path.resolve()
    top = result.stdout.strip()
    return Path(top).resolve() if result.returncode == 0 and top else path.resolve()


def run_rust(
    crate: Path,
    *,
    out: Path | None = None,
    work: Path | None = None,
    root: Path | None = None,
    jobs: int | None = None,
    runner: Runner | None = None,
    archive: Path | None = None,
    keep_profiles: bool = False,
    env: Mapping[str, str] | None = None,
    log: Callable[[str], None] = lambda _msg: None,
) -> RunSummary:
    """Run the crate's tests with per-test coverage and write a neutral bundle (all three stages).

    ``out`` defaults to ``<crate>/.coverset/neutral``, ``work`` (profiles, exports, the capture) to
    ``<crate>/.coverset/rust``, ``root`` to the git work tree's top, ``jobs`` to the CPU count,
    ``runner`` to nextest when ``cargo nextest`` is installed, else libtest. The ``.profraw`` and
    ``.profdata`` files (about 50 KB per test) are deleted once exported unless ``keep_profiles``;
    the capture keeps only the exports.
    """
    crate = crate.resolve()
    if not (crate / "Cargo.toml").is_file():
        raise CaptureError(f"{crate} has no Cargo.toml")
    out = (out or crate / ".coverset" / "neutral").resolve()
    work = (work or crate / ".coverset" / "rust").resolve()
    root = (root or repo_root_of(crate, env)).resolve()
    jobs = max(int(jobs or os.cpu_count() or 4), 1)
    base_env = _tool_env(env)
    tools = find_llvm_tools(base_env)
    version = nextest_version(base_env)
    if runner is None:
        runner = "nextest" if version else "libtest"
    if runner == "nextest" and version is None:
        raise ToolchainError(
            "cargo-nextest is not installed: `cargo install --locked cargo-nextest`, or pass --runner libtest"
        )
    work.mkdir(parents=True, exist_ok=True)
    _prepare_work(work)
    triple = host_triple(base_env)
    script, temp = _place_runner(work) if runner == "nextest" else (None, None)
    child_env = capture_env(work, triple, base_env, runner=script)
    if script is None:  # libtest: coverset runs the binaries itself, cargo only builds them
        child_env.pop(_runner_var(triple), None)
    child_env["CARGO_TARGET_DIR"] = str(crate / "target" / "coverset")
    started = time.monotonic()
    try:
        layout = _cargo_layout(crate, child_env)
        stage = _run_nextest if runner == "nextest" else _run_libtest
        binaries, tests, timings, executables = stage(crate, work, jobs, child_env, log, layout)
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
    timings.update(_export(tools, binaries, tests, work, jobs, log, executables))
    if not keep_profiles:
        shutil.rmtree(work / "profiles", ignore_errors=True)
    capture = Capture(
        directory=work,
        root=root,
        runner=runner,
        runner_version=version if runner == "nextest" else None,
        rustc=rustc_version(base_env),
        binaries=binaries,
        tests=tests,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        jobs=jobs,
        timings_s=timings,
    )
    _write_capture(capture)
    log("converting the capture into a neutral bundle")
    clock = time.monotonic()
    bundle = convert_capture(work, root=root)
    write_neutral(bundle, out, created_at=capture.created_at, compress=True)
    timings["convert"] = round(time.monotonic() - clock, 3)
    timings["total"] = round(time.monotonic() - started, 3)
    if archive is not None:
        pack(out, archive)
    outcomes: dict[str, int] = {}
    for test in tests:
        label = test.outcome or "not run"
        outcomes[label] = outcomes.get(label, 0) + 1
    _write_capture(dataclasses.replace(capture, timings_s=timings))
    return RunSummary(out, work, root, runner, len(tests), outcomes, timings, archive)
