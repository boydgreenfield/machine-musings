"""``coverset doctor``: what coverset will do with a repository, before it runs a single test.

Onboarding a repository used to mean finding out at the first run that its environment could not be
described, its package was not on ``PYTHONPATH`` or its ``addopts`` broke collection. The doctor makes
every decision coverset would make, says how it made it, and checks the ones that can only be checked
by trying:

* **language**: Python (``PythonAdapter.detect``), or a TypeScript repo that belongs in upload mode;
* **Python version** and where it came from (``[tool.coverset]``, ``.python-version``,
  ``requires-python``, the default);
* **environment**: the source (``uv.lock``, requirement files, ``poetry.lock``, ``Pipfile.lock``,
  ``[project]``, ``setup.cfg``/``setup.py``), why it was chosen, what was dropped from it, and the first
  requirement lines of the resolution (:mod:`coverset.lang.python_env`);
* **source packages** and how they were inferred, **test paths**, pytest's configuration file, the
  ``addopts`` words that clash with how coverset runs pytest, and the exact collect and shard commands;
* **install mode** (``[tool.coverset] install``) and the signals behind ``auto``;
* **collection**, unless ``--no-collect``: the environment is built with the LocalExecutor (the cached
  venv a local run would use), the project installed if it needs to be, the working tree (tracked
  and untracked files that git does not ignore) copied into a throwaway worker, every source
  package imported once from ``PYTHONPATH``, and the planner's own collect command run, reporting the
  collected count and every collection error;
* **findings**, each with a fix, and a verdict: ``ready``, ``ready, with warnings`` or ``not ready``.

The repository is never modified: builds happen in temporary copies, and ``--write-config`` appends
the suggested ``[tool.coverset]`` section to a file outside the repository (default
``<data dir>/doctor/<repo name>.toml``). Collection runs the repository's test-collection code
(``conftest.py``, test modules' imports) as your user, exactly as ``pytest --collect-only`` would.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import click

from coverset.lang.python import (
    CoversetConfig,
    EnvReport,
    PythonAdapter,
    addopts_conflicts,
    explain_config,
    pytest_config,
    suggested_addopts,
)
from coverset.lang.python_env import (
    MAX_PYPROJECT_BYTES,
    ConfigError,
    EnvSpecError,
    install_needed,
    load_pyproject,
    project_metadata,
    read_repo_text,
)

__all__ = [
    "DoctorReport",
    "Finding",
    "append_config",
    "diagnose",
    "doctor_command",
    "doctor_group",
    "render_text",
    "suggested_config",
    "worktree_archive",
]

type Level = Literal["error", "warning", "info"]

FIRST_REQUIREMENTS = 10
LARGE_REPO_FILES = 20_000
LARGE_REPO_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MANY_TESTS = 5_000
COLLECT_TIMEOUT_S = 600.0
IMPORT_TIMEOUT_S = 120.0
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".venv",
        "venv",
        "env",
        ".tox",
        ".nox",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".coverset",
        ".eggs",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing the doctor wants you to know, with the fix when there is one."""

    level: Level
    code: str
    message: str
    fix: str | None = None


@dataclass(slots=True)
class Collection:
    """What collecting the tests in a throwaway worker showed."""

    ran: bool = False
    skipped: str | None = None
    exit_code: int | None = None
    collected: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    imports: list[dict[str, Any]] = field(default_factory=list)
    env_seconds: float = 0.0
    install_seconds: float = 0.0
    collect_seconds: float = 0.0
    output_tail: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DoctorReport:
    """Everything ``coverset doctor`` found (``--json`` prints it as is)."""

    repo: str
    language: str
    verdict: str = "not ready"
    python: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    tests: dict[str, Any] = field(default_factory=dict)
    install: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, list[str]] = field(default_factory=dict)
    invariants: dict[str, Any] = field(default_factory=dict)
    tracked_globs: list[str] = field(default_factory=list)
    size: dict[str, int] = field(default_factory=dict)
    collection: Collection = field(default_factory=Collection)
    findings: list[Finding] = field(default_factory=list)
    suggested_config: str = ""
    _config: CoversetConfig | None = field(default=None, repr=False)
    _adapter: PythonAdapter | None = field(default=None, repr=False)

    def add(self, level: Level, code: str, message: str, fix: str | None = None) -> None:
        """Record a finding."""
        self.findings.append(Finding(level, code, message, fix))

    def to_json(self) -> dict[str, Any]:
        """A JSON-serialisable dict of the report (the private fields left out)."""
        return {k: v for k, v in dataclasses.asdict(self).items() if not k.startswith("_")}


# -- the diagnosis ----------------------------------------------------------------------------------------


def diagnose(
    repo: Path,
    *,
    adapter: PythonAdapter | None = None,
    collect: bool = True,
    timeout_s: float = COLLECT_TIMEOUT_S,
) -> DoctorReport:
    """Build the :class:`DoctorReport` for the repository at ``repo`` (see the module docstring)."""
    return asyncio.run(diagnose_async(repo, adapter=adapter, collect=collect, timeout_s=timeout_s))


async def diagnose_async(
    repo: Path,
    *,
    adapter: PythonAdapter | None = None,
    collect: bool = True,
    timeout_s: float = COLLECT_TIMEOUT_S,
) -> DoctorReport:
    """:func:`diagnose` for callers already on an event loop (an agent interface, the web app).

    The static part (configuration, environment resolution, file walks) runs in a thread.
    """
    report = await asyncio.to_thread(_static_report, repo, adapter or PythonAdapter())
    if report.language != "python" or not report.commands:
        if not collect:
            report.collection.skipped = "--no-collect"
        elif report.language == "python":
            report.collection.skipped = "no environment"
        return _finish(report)
    config, adapter = report._config, report._adapter
    if collect:
        try:
            await _collect(report, Path(report.repo), config, adapter, timeout_s)
        except Exception as exc:  # the report is still worth printing
            report.collection.skipped = f"collection failed to start: {type(exc).__name__}: {exc}"
            report.add(
                "error",
                "collect-setup",
                f"could not build the environment or a worker: {exc}",
                "read the message; `coverset doctor --no-collect` still reports everything else",
            )
    else:
        report.collection.skipped = "--no-collect"
    _collection_findings(report, config)
    return _finish(report)


def _static_report(repo: Path, adapter: PythonAdapter) -> DoctorReport:
    """Everything the doctor decides without building an environment or running pytest."""
    repo = repo.resolve()
    adapter = adapter or PythonAdapter()
    report = DoctorReport(repo=str(repo), language="unknown")
    if not repo.is_dir():
        report.add("error", "no-repo", f"{repo} is not a directory")
        return _finish(report)
    if not adapter.detect(repo):
        if (repo / "Cargo.toml").is_file():
            report.language = "rust"
            report.add(
                "error",
                "not-python",
                "a Rust repository: coverset does not run its tests",
                "use upload mode with a neutral bundle from your CI (docs/rust.md, docs/upload.md)",
            )
        elif (repo / "package.json").is_file():
            report.language = "typescript"
            report.add(
                "error",
                "not-python",
                "a JavaScript/TypeScript repository: coverset does not run its tests",
                "use upload mode: run Vitest with the coverset-vitest reporter in your CI and upload the "
                "neutral bundle (docs/typescript.md, docs/getting-started.md)",
            )
        else:
            report.add(
                "error", "not-python", "no pyproject.toml, setup.cfg, setup.py, requirements or *.py found"
            )
        return _finish(report)
    report.language = "python"
    try:
        text = read_repo_text(repo, "pyproject.toml", MAX_PYPROJECT_BYTES) or ""
        config, how = explain_config(text, repo_root=repo)
    except ConfigError as exc:
        report.add(
            "error", "config", f"[tool.coverset] or a file it reads is invalid: {exc}", "fix the value named"
        )
        return _finish(report)
    _describe_config(report, repo, config, how)
    env = _describe_environment(report, repo, adapter, config)
    _describe_tests(report, repo, config, env, how)
    _describe_size(report, repo)
    if env is not None:
        _describe_commands(report, config, env)
    report.suggested_config = suggested_config(report, config, env)
    report._config, report._adapter = config, adapter
    return report


def _finish(report: DoctorReport) -> DoctorReport:
    levels = {f.level for f in report.findings}
    report.verdict = (
        "not ready" if "error" in levels else "ready, with warnings" if "warning" in levels else "ready"
    )
    order = {"error": 0, "warning": 1, "info": 2}
    report.findings.sort(key=lambda f: order[f.level])
    return report


def _describe_config(report: DoctorReport, repo: Path, config: CoversetConfig, how: dict[str, str]) -> None:
    report.python = {"version": config.python, "how": how.get("python", "default")}
    report.source = {
        "source_root": config.source_root,
        "how_root": how.get("source_root", "default"),
        "packages": list(config.source),
        "how_packages": how.get("source", "default"),
    }
    report.invariants = {"path": config.invariants, "exists": (repo / config.invariants).is_file()}
    report.tracked_globs = list(config.tracked_globs)
    if config.unknown_keys:
        report.add(
            "warning",
            "unknown-keys",
            "[tool.coverset] has keys this version ignores: " + ", ".join(config.unknown_keys),
            "check the spelling against docs/getting-started.md",
        )
    if not config.source:
        report.add(
            "error",
            "no-source",
            f"no source packages found under {config.source_root!r}: coverage would measure nothing",
            'set [tool.coverset] source = ["your_package"] (and source_root if it lives in a subdirectory)',
        )
    if "allows no 3.12+" in report.python["how"]:
        report.add(
            "warning",
            "python",
            f"{report.python['how']}; coverset needs Python 3.12 or later and will use {config.python}",
            'set [tool.coverset] python = "3.12" if the code runs there, or upgrade the project',
        )
    if not report.invariants["exists"]:
        report.add(
            "info",
            "invariants",
            f"no {config.invariants}: invariants are optional",
            "declare invariants later (docs/invariants.md)",
        )


def _describe_environment(
    report: DoctorReport, repo: Path, adapter: PythonAdapter, config: CoversetConfig
) -> EnvReport | None:
    started = time.perf_counter()
    try:
        env = adapter.describe_env(repo, {"python": config.python})
    except (EnvSpecError, ConfigError) as exc:
        report.environment = {"error": str(exc)}
        fix = "commit a lock (`uv lock`) or set [tool.coverset] requirements = [...]"
        if "uv pip compile" in str(exc):
            fix = "read uv's message: a missing package, a version conflict, an sdist-only dependency or no network"
        elif "uv is not installed" in str(exc):
            fix = "install uv (https://docs.astral.sh/uv/) on the machine running coverset"
        report.add("error", "environment", f"the environment cannot be described: {exc}", fix)
        return None
    lines = [
        line
        for line in env.resolved.requirements_txt.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    report.environment = {
        "source": env.source.kind,
        "reason": env.source.reason,
        "inputs": list(env.resolved.files),
        "resolved_by": env.resolved.resolved_by,
        "seconds": round(time.perf_counter() - started, 3),
        "requirements": len(lines),
        "first_requirements": lines[:FIRST_REQUIREMENTS],
        "notes": list(env.resolved.notes),
        "env_hash": env.spec.env_hash,
        "local_packages": [
            f"{p.name} ({p.kind}: {p.path}{' ; ' + p.marker if p.marker else ''})" for p in env.resolved.local
        ],
    }
    report.install = {
        "setting": env.install.setting,
        "mode": env.install.mode,
        "reasons": list(env.install.reasons),
        "build_backend": env.install.build_backend,
        "build_requires": list(env.install.build_requires) if env.install.mode != "none" else [],
    }
    if env.source.kind not in ("uv.lock", "poetry", "pipenv"):
        report.add(
            "warning",
            "no-lock",
            f"no lock: the {env.source.kind} source is pinned with `uv pip compile` (cached by its inputs; "
            "the first resolution of new inputs needs network)",
            "commit a lock (`uv lock`) for environments that only change when you change them",
        )
    for note in env.resolved.notes[:12]:
        report.add("info", "env-note", note)
    from coverset.executors.env_export import direct_references

    refs = direct_references(env.resolved.requirements_txt)
    if refs:
        report.add(
            "warning",
            "direct-references",
            f"{len(refs)} requirement(s) come from a URL or VCS ({refs[0]}...): an untrusted pull request "
            "that changes the environment is never built",
            "publish those packages to an index, or accept that untrusted PRs touching them need a maintainer",
        )
    doc = load_pyproject(repo)
    meta = project_metadata(repo, doc)
    if meta.extras:
        report.add("info", "extras", "every extra is installed: " + ", ".join(sorted(meta.extras)))
    lowered = env.resolved.requirements_txt.lower()
    if "pytest-xdist" in lowered:
        report.add(
            "info",
            "xdist",
            "pytest-xdist is installed: coverset adds --numprocesses=0 (it shards the suite itself)",
        )
    if "pytest-cov" in lowered:
        report.add(
            "info",
            "pytest-cov",
            "pytest-cov is installed: coverset adds --no-cov (it measures coverage itself)",
        )
    if not re.search(r"(?m)^pytest(\[|=|>|<|~|!|\s|;|$)", env.resolved.requirements_txt):
        report.add(
            "info",
            "no-pytest",
            "pytest is not in the requirements: coverset adds the pytest version it is tested with",
        )
    for warning in env.install.warnings:
        report.add(
            "warning", "compiled", warning, "re-snapshot after merging C/Rust changes (`coverset warm`)"
        )
    lfs = _lfs_tracked(repo, [p.path for p in env.resolved.local if p.kind == "file"])
    if lfs:
        report.add(
            "error",
            "lfs-local-packages",
            "local packages stored with Git LFS: " + ", ".join(lfs) + ". coverset exports commits with "
            "`git archive` from a bare mirror, which yields LFS pointer files, so these are skipped and "
            "tests that import them fail (this working tree has the real files, so collection here may pass)",
            "publish them to a private index (UV_EXTRA_INDEX_URL on the control plane) and depend on them "
            "by version, or commit them without LFS",
        )
    if env.resolved.local:
        names = sorted({p.name for p in env.resolved.local})
        report.add(
            "info",
            "local-packages",
            f"{len(names)} package(s) live in the repository and are installed from the checkout with "
            f"--no-deps: {', '.join(names)}",
            "on Modal they are installed in the trusted snapshot: run `coverset warm --repo <name>` first",
        )
    if env.install.mode != "none":
        report.add(
            "info",
            "install",
            f"the project is installed ({env.install.mode}): " + "; ".join(env.install.reasons),
            "on Modal, run `coverset warm --repo <name>` so the first pull request finds a trusted snapshot",
        )
    elif meta.scripts:
        report.add(
            "info",
            "console-scripts",
            "console scripts ("
            + ", ".join(meta.scripts[:5])
            + ") are not on PATH unless the project is installed",
            'set [tool.coverset] install = "wheel" if tests run them by name',
        )
    return env


def _lfs_tracked(repo: Path, paths: list[str]) -> list[str]:
    """The ``paths`` that git stores with the LFS filter (``git check-attr``), empty without git."""
    if not paths or not (repo / ".git").exists() or shutil.which("git") is None:
        return []
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo), "check-attr", "-z", "filter", "--stdin"],
            input="\0".join(paths).encode() + b"\0",
            capture_output=True,
            timeout=60,
            check=False,
            env={"PATH": os.environ.get("PATH", os.defpath), "GIT_TERMINAL_PROMPT": "0", "HOME": str(repo)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    fields = proc.stdout.decode("utf-8", "replace").split("\0")
    return sorted({fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] == "lfs"})


def _describe_tests(
    report: DoctorReport, repo: Path, config: CoversetConfig, env: EnvReport | None, how: dict[str, str]
) -> None:
    try:
        ini = pytest_config(repo)
    except ConfigError as exc:
        report.add("error", "pytest-config", f"pytest's configuration does not parse: {exc}")
        ini = None
    requirements = env.resolved.requirements_txt if env is not None else ""
    conflicts = addopts_conflicts(ini.addopts, requirements) if ini is not None else []
    report.tests = {
        "testpaths": list(config.testpaths),
        "how": how.get("testpaths", "default"),
        "pytest_config": ini.file if ini is not None else None,
        "addopts": list(ini.addopts) if ini is not None else [],
        "conflicts": [{"option": word, "problem": problem} for word, problem in conflicts],
        "pytest_args": list(config.pytest_args),
    }
    missing = [p for p in config.testpaths if not (repo / p).exists()]
    if missing:
        report.add(
            "error",
            "testpaths",
            "test paths that do not exist: " + ", ".join(missing),
            "set [tool.coverset] testpaths to the directories that hold the tests",
        )
    if not config.testpaths:
        report.add(
            "warning",
            "no-testpaths",
            "no tests/, test/ or testing/ directory and no testpaths: pytest collects from the repository root",
            'set [tool.coverset] testpaths = ["path/to/tests"] to keep collection fast and predictable',
        )
    if ini is not None and conflicts:
        cleaned = suggested_addopts(ini.addopts, conflicts)
        for word, problem in conflicts:
            level: Level = "error" if "usage error" in problem else "warning"
            report.add(
                level,
                "addopts",
                f"addopts {word!r} in {ini.file}: {problem}",
                f"remove it from addopts in {ini.file} (the rest would read: {cleaned or '(empty)'})",
            )
    if config.pytest_args:
        report.add(
            "warning",
            "pytest-args-pending",
            "[tool.coverset] pytest_args "
            + " ".join(config.pytest_args)
            + " are validated but not yet passed to real collects and shards (the runner and planner "
            "do not forward them), so the commands below and this collection run without them",
            "until that lands, put these options in the repository's own pytest addopts",
        )
    if (repo / "conftest.py").is_file():
        report.add(
            "info",
            "root-conftest",
            "conftest.py at the repository root: every test depends on it, and a change to it marks every "
            "test impacted (tracked_globs)",
        )


def _describe_size(report: DoctorReport, repo: Path) -> None:
    files = _worktree_files(repo)
    attributes = repo / ".gitattributes"
    lfs = (
        set(_lfs_tracked(repo, files))
        if attributes.is_file() and "lfs" in attributes.read_text("utf-8", "replace")
        else set()
    )
    total = 0
    py = 0
    sizes: list[tuple[int, str]] = []
    for rel in files:
        with contextlib.suppress(OSError):
            size = (
                134 if rel in lfs else (repo / rel).lstat().st_size
            )  # an LFS pointer, as git archive exports it
            total += size
            sizes.append((size, rel))
        py += rel.endswith(".py")
    report.size = {"files": len(files), "bytes": total, "python_files": py, "lfs_files": len(lfs)}
    if len(files) > LARGE_REPO_FILES or total > LARGE_REPO_BYTES:
        biggest = ", ".join(f"{rel} ({size / 1e6:.0f} MB)" for size, rel in sorted(sizes, reverse=True)[:3])
        report.add(
            "warning",
            "large-repo",
            f"large repository ({len(files):,} files, {total / 1e6:.0f} MB; largest: {biggest}): every "
            "worker receives the whole archive",
            "keep large binaries out of git (Git LFS pointers are small), or expect slower worker start-up",
        )


def _describe_commands(report: DoctorReport, config: CoversetConfig, env: EnvReport) -> None:
    requirements = env.resolved.requirements_txt.lower()
    extra = {
        "python": "python",
        "sources": config.source,
        "trace_modules": config.trace_modules,
        "shapes": "on",
        "stop_file": "run/STOP",
        "xdist": "pytest-xdist" in requirements,
        "pytest_cov": "pytest-cov" in requirements,
        # no "pytest_args": the runner's _extra() does not forward them yet (see _describe_tests)
    }
    scores = "run/shard.scores.json" if config.reorder else None
    shard = PythonAdapter().test_command("run/shard.args", scores, "run/out", extra) + ["--rootdir=repo"]
    report.commands = {"collect": _collect_argv("repo", "run/collect", config), "shard": shard}


def _collect_argv(repo: str, out: str, config: CoversetConfig) -> list[str]:
    """The planner's collect command (``orchestrator/planner.py``, ``_collect``) for these paths.

    It must stay identical to the planner's, or the doctor's verdict would describe a run that
    never happens: ``[tool.coverset] pytest_args`` are left out because the planner does not pass
    them yet (``_describe_tests`` warns when they are set).
    """
    return [
        "python",
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--rootdir={repo}",
        f"--coverset-out={out}",
        "--coverset-shapes=off",
        "--continue-on-collection-errors",
        *config.testpaths,
    ]


# -- collection in a throwaway worker ---------------------------------------------------------------------


async def _collect(
    report: DoctorReport, repo: Path, config: CoversetConfig, adapter: PythonAdapter, timeout_s: float
) -> None:
    from coverset.executors.base import Resources
    from coverset.executors.env_export import build_env_spec, build_plugin_wheel
    from coverset.executors.local import LocalExecutor
    from coverset.orchestrator.planner import (
        LAYOUT,
        REPO_DIR,
        parse_collect_json,
        parse_collect_listing,
        worker_env,
    )

    result = report.collection
    tarball, _, _ = worktree_archive(repo)
    started = time.perf_counter()
    wheel = await asyncio.to_thread(build_plugin_wheel)
    spec = await asyncio.to_thread(build_env_spec, adapter, repo, {"python": config.python}, wheel)
    executor = LocalExecutor(max_workers=1, sweep=False)
    env_ref = await executor.ensure_env(spec)
    result.env_seconds = round(time.perf_counter() - started, 3)
    run_id = f"doctor-{uuid.uuid4().hex[:12]}"
    checkout = Path(tempfile.mkdtemp(prefix="coverset-doctor-"))
    worker = None
    try:
        if install_needed(spec):
            started = time.perf_counter()
            await asyncio.to_thread(_extract, tarball, checkout)
            sha = hashlib.sha256(tarball).hexdigest()
            await executor.install_project(env_ref, checkout, sha=sha, run_id=run_id)
            result.install_seconds = round(time.perf_counter() - started, 3)
        (worker,) = await executor.acquire(env_ref, 1, Resources(), int(timeout_s), {"coverset_run": run_id})
        await worker.put_tree(tarball, REPO_DIR)
        nonce = secrets.token_hex(16)
        child_env = worker_env(worker, config, nonce, {})
        for package in config.source:
            lines: deque[str] = deque(maxlen=8)
            probe = await worker.run(
                ["python", "-c", f"import {package}"],
                cwd=REPO_DIR,
                env=child_env,
                timeout_s=IMPORT_TIMEOUT_S,
                on_line=lambda text, _s, lines=lines: lines.append(text),
                stdout_cap=1 << 20,
            )
            error = next((line for line in reversed(lines) if line.strip()), "") if probe.exit_code else ""
            result.imports.append({"package": package, "ok": probe.exit_code == 0, "error": error[:300]})
        tail: deque[str] = deque(maxlen=40)
        listing: list[str] = []
        argv = _collect_argv(worker.path(REPO_DIR), worker.path(LAYOUT.collect_dir), config)

        def on_line(text: str, stream: str) -> None:
            tail.append(text)
            if stream == "stdout":
                listing.append(text)

        started = time.perf_counter()
        run = await worker.run(
            argv, cwd=REPO_DIR, env=child_env, timeout_s=timeout_s, on_line=on_line, stdout_cap=64 << 20
        )
        result.collect_seconds = round(time.perf_counter() - started, 3)
        result.ran = True
        result.exit_code = -1 if run.timed_out else run.exit_code
        result.output_tail = [line[:300] for line in tail][-20:]
        try:
            items, errors = parse_collect_json(await worker.get_file(f"{LAYOUT.collect_dir}/collect.json"))
        except Exception:
            items, errors = parse_collect_listing(listing), []
        result.collected = len(items)
        result.errors = [
            {"path": str(e.get("nodeid") or e.get("path") or "?"), "message": _first_line(e)} for e in errors
        ]
    finally:
        if worker is not None:
            await worker.release()
        await executor.close()
        shutil.rmtree(checkout, ignore_errors=True)


def _first_line(error: dict[str, Any]) -> str:
    text = str(error.get("longrepr") or error.get("message") or error.get("error") or "")
    lines = [line for line in text.splitlines() if line.strip()]
    interesting = [line for line in lines if line.lstrip().startswith("E ")]
    return (interesting[-1] if interesting else lines[-1] if lines else "")[:300]


def _collection_findings(report: DoctorReport, config: CoversetConfig) -> None:
    result = report.collection
    if not result.ran:
        return
    failed = [i for i in result.imports if not i["ok"]]
    for item in failed:
        fix = (
            'set [tool.coverset] install = "wheel" (the package needs to be installed)'
            if report.install.get("mode") == "none"
            else "read the error: a missing dependency belongs in the requirements"
        )
        report.add(
            "error", "import", f"`import {item['package']}` fails from PYTHONPATH: {item['error']}", fix
        )
    if result.exit_code == -1:
        report.add("error", "collect-timeout", "collection timed out", "raise --timeout or narrow testpaths")
    elif result.exit_code not in (0, 1, 2, 5):
        tail = " | ".join(result.output_tail[-3:])
        report.add(
            "error",
            "collect-usage",
            f"pytest exited {result.exit_code} while collecting: {tail}",
            "a usage error usually comes from addopts or a plugin option; see the addopts findings",
        )
    elif result.errors:
        level: Level = "error" if result.collected == 0 else "warning"
        report.add(
            level,
            "collect-errors",
            f"{len(result.errors)} test module(s) fail to import (each becomes a failure of every run): "
            + "; ".join(f"{e['path']}: {e['message']}" for e in result.errors[:3]),
            "a missing test dependency belongs in the requirements (or a dev/test dependency group)",
        )
    elif result.collected == 0:
        report.add(
            "error", "no-tests", "no tests were collected", "check testpaths and pytest's python_files"
        )
    if result.collected > MANY_TESTS:
        report.add(
            "info",
            "many-tests",
            f"{result.collected:,} tests: the first run is dealt into shards by count until durations are known",
            f"raise [tool.coverset] max_shards (now {config.max_shards}) if runs are slow",
        )


# -- the working tree --------------------------------------------------------------------------------------


def _worktree_files(repo: Path) -> list[str]:
    """Tracked and untracked-but-not-ignored files (git), else every file outside build/venv folders."""
    if (repo / ".git").exists() and shutil.which("git"):
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(repo),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=120,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", os.defpath),
                    "GIT_TERMINAL_PROMPT": "0",
                    "HOME": str(repo),
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0:
            names = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
            return sorted({n for n in names if n and ((repo / n).is_symlink() or (repo / n).is_file())})
    found: list[str] = []
    for folder, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.endswith(".egg-info"))
        rel = Path(folder).relative_to(repo)
        found += [str(rel / name) if str(rel) != "." else name for name in sorted(files)]
    return found


def worktree_archive(repo: Path, *, max_bytes: int = MAX_ARCHIVE_BYTES) -> tuple[bytes, int, int]:
    """A ``.tar.gz`` of the working tree (see :func:`_worktree_files`): (bytes, files, raw bytes)."""
    buffer = io.BytesIO()
    total = 0
    files = _worktree_files(repo)
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=1) as tar:
        for rel in files:
            path = repo / rel
            try:
                info = tar.gettarinfo(str(path), arcname=rel)
            except OSError:
                continue
            if info.isfile():
                total += info.size
                if total > max_bytes:
                    raise click.ClickException(f"the working tree is larger than {max_bytes} bytes")
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
            elif info.issym():
                tar.addfile(info)
    return buffer.getvalue(), len(files), total


def _extract(tarball: bytes, target: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        tar.extractall(target, filter="data")


# -- output ---------------------------------------------------------------------------------------------


def suggested_config(report: DoctorReport, config: CoversetConfig, env: EnvReport | None) -> str:
    """A ``[tool.coverset]`` section with every value the doctor inferred (comments say why)."""

    def q(value: str) -> str:
        return json.dumps(value)

    def arr(values: list[str] | tuple[str, ...]) -> str:
        return "[" + ", ".join(q(v) for v in values) + "]"

    lines = [
        f"# Suggested by `coverset doctor` for {Path(report.repo).name} on {datetime.now(UTC):%Y-%m-%d}",
        "[tool.coverset]",
        f"source = {arr(config.source)}  # {report.source.get('how_packages', '')}",
        f"source_root = {q(config.source_root)}  # {report.source.get('how_root', '')}",
        f"testpaths = {arr(config.testpaths)}",
        f"python = {q(config.python)}  # {report.python.get('how', '')}",
    ]
    if env is not None:
        if env.source.kind == "requirements":
            lines.append(f"requirements = {arr(list(env.source.inputs))}  # {env.source.reason}")
        mode = env.install.mode
        lines.append(f"install = {q(mode)}  # {'; '.join(env.install.reasons)[:160]}")
    conflicts = report.tests.get("conflicts") or []
    if conflicts:
        cleaned = suggested_addopts(
            tuple(report.tests.get("addopts", ())), [(c["option"], c["problem"]) for c in conflicts]
        )
        # pytest_args are not forwarded yet, so an --override-ini here would fix the doctor's view only
        where = report.tests.get("pytest_config") or "the pytest configuration"
        comment = f"# edit addopts in {where} (not here) to read: {cleaned or '(empty)'}"
        lines.append(" ".join(comment.split()))  # one line, whatever the repository's addopts hold
    if config.pytest_args:
        lines.append(f"pytest_args = {arr(config.pytest_args)}  # not forwarded to runs yet")
    if report.invariants.get("exists"):
        lines.append(f"invariants = {q(config.invariants)}")
    return "\n".join(lines) + "\n"


_MARK = {"error": "x", "warning": "!", "info": "i"}


def render_text(report: DoctorReport) -> str:
    """The human-readable report."""
    out: list[str] = [f"coverset doctor: {report.repo}", ""]

    def row(key: str, value: object) -> None:
        out.append(f"  {key:<20} {value}")

    out.append("Repository")
    row("language", report.language)
    if report.size:
        row(
            "size",
            f"{report.size['files']:,} files, {report.size['python_files']:,} Python, {report.size['bytes'] / 1e6:.1f} MB",
        )
    if report.python:
        row("python", f"{report.python['version']}  ({report.python['how']})")
    if report.environment:
        out += ["", "Environment"]
        env = report.environment
        if "error" in env:
            row("error", env["error"])
        else:
            row("source", f"{env['source']}  ({env['reason']})")
            row("inputs", ", ".join(env["inputs"]) or "-")
            row("resolved by", f"{env['resolved_by']} in {env['seconds']:.2f} s")
            row(
                "requirements",
                f"{env['requirements']} pinned (first {min(env['requirements'], FIRST_REQUIREMENTS)}):",
            )
            out += [f"{'':<23}{line}" for line in env["first_requirements"]]
            for local in env.get("local_packages", []):
                row("local package", local)
            row("env hash", env["env_hash"][:16])
    if report.source:
        out += ["", "Source"]
        row("source_root", f"{report.source['source_root']}  ({report.source['how_root']})")
        row("packages", f"{', '.join(report.source['packages']) or '-'}  ({report.source['how_packages']})")
    if report.install:
        row(
            "install",
            f"{report.install['mode']} (setting {report.install['setting']}): {'; '.join(report.install['reasons'])}",
        )
    if report.tests:
        out += ["", "Tests"]
        row("testpaths", f"{', '.join(report.tests['testpaths']) or '(rootdir)'}  ({report.tests['how']})")
        row("pytest config", report.tests["pytest_config"] or "none")
        row("addopts", " ".join(report.tests["addopts"]) or "-")
        row(
            "invariants",
            f"{report.invariants.get('path')} ({'present' if report.invariants.get('exists') else 'absent'})",
        )
        row("tracked globs", ", ".join(report.tracked_globs))
    if report.commands:
        out += ["", "Commands (paths relative to the worker)"]
        row("collect", " ".join(report.commands["collect"]))
        row("shard", " ".join(report.commands["shard"]))
    out += ["", "Collection"]
    c = report.collection
    if not c.ran:
        row("skipped", c.skipped or "-")
    else:
        row(
            "environment",
            f"ready in {c.env_seconds:.1f} s"
            + (f", project installed in {c.install_seconds:.1f} s" if c.install_seconds else ""),
        )
        for item in c.imports:
            row(f"import {item['package']}", "ok" if item["ok"] else f"FAILS: {item['error']}")
        row("collected", f"{c.collected:,} tests in {c.collect_seconds:.1f} s (exit {c.exit_code})")
        for error in c.errors[:10]:
            row("error", f"{error['path']}: {error['message']}")
        if len(c.errors) > 10:
            row("", f"... and {len(c.errors) - 10} more")
    out += ["", "Findings"]
    if not report.findings:
        out.append("  none")
    for finding in report.findings:
        out.append(f"  [{_MARK[finding.level]}] {finding.level:<7} {finding.message}")
        if finding.fix:
            out.append(f"{'':<14}fix: {finding.fix}")
    out += ["", f"Verdict: {report.verdict}"]
    if report.suggested_config:
        out += [
            "",
            "Suggested [tool.coverset] (--write-config appends it to a file outside the repository):",
            "",
        ]
        out += [f"  {line}" for line in report.suggested_config.splitlines()]
    return "\n".join(out) + "\n"


def _default_config_out(repo: Path) -> Path:
    """``<data dir>/doctor/<repo>.toml``, or under coverset's cache when the data dir is in the repo.

    Without settings the data dir defaults to ``./data``, which is inside the repository when the
    doctor runs from its root; the cache directory (``~/.cache/coverset``) never is.
    """
    from coverset.commands.common import load
    from coverset.executors.base import cache_root

    try:
        data_dir = load().data_dir
    except Exception:
        data_dir = Path(os.environ.get("COVERSET_DATA_DIR", "data"))
    name = f"{repo.name or 'repo'}.toml"
    target = data_dir.expanduser().resolve() / "doctor" / name
    if target.is_relative_to(repo.resolve()):
        target = cache_root().expanduser().resolve() / "doctor" / name
    return target


def _config_target(repo: Path, path: Path) -> Path:
    """``path`` resolved, refused when it lies inside the repository (the doctor never writes there)."""
    target = path.expanduser().resolve()
    if target.is_relative_to(repo.resolve()):
        raise click.ClickException(f"{target} is inside the repository; the doctor never writes there")
    return target


def append_config(report: DoctorReport, repo: Path, path: Path) -> Path:
    """Append the suggested section to ``path``, which must lie outside the repository."""
    target = _config_target(repo, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(("\n" if target.stat().st_size else "") + report.suggested_config)
    return target


@click.command(name="doctor")
@click.option(
    "--repo",
    "repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="The repository (a working tree) to examine.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
@click.option("--no-collect", is_flag=True, help="Skip building the environment and collecting the tests.")
@click.option(
    "--offline", is_flag=True, help="Never go online to resolve requirements (cached resolutions only)."
)
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=COLLECT_TIMEOUT_S,
    show_default=True,
    help="Collection timeout in seconds.",
)
@click.option(
    "--write-config", is_flag=True, help="Append the suggested [tool.coverset] section to --config-out."
)
@click.option(
    "--config-out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Where --write-config appends (default <data dir>/doctor/<repo name>.toml, or "
        "~/.cache/coverset/doctor/ when the data dir is inside the repo; never inside the repo)."
    ),
)
def doctor_command(
    repo: Path,
    as_json: bool,
    no_collect: bool,
    offline: bool,
    timeout_s: float,
    write_config: bool,
    config_out: Path | None,
) -> None:
    """Report what coverset would do with a repository, without running its tests.

    Exits 0 when the repository is ready (warnings included) and 1 when a finding is an error.
    """
    target = None
    if write_config:  # refuse a target inside the repository before the (slow) diagnosis
        target = _config_target(repo, config_out or _default_config_out(repo.resolve()))
    report = diagnose(
        repo, adapter=PythonAdapter(offline_only=offline), collect=not no_collect, timeout_s=timeout_s
    )
    written = None
    if target is not None and report.suggested_config:
        written = append_config(report, repo, target)
    if as_json:
        payload = report.to_json()
        if written is not None:
            payload["config_written_to"] = str(written)
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(render_text(report), nl=False)
        if written is not None:
            click.echo(f"\nSuggested configuration appended to {written}")
    if report.verdict == "not ready":
        raise SystemExit(1)


doctor_group = doctor_command
"""The name ``coverset.commands.register_commands`` registers as ``coverset doctor``."""
