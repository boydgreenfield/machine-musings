"""Environment export shared by every executor: the plugin wheel and the :class:`EnvSpec`.

An environment is the target repo's pinned third-party requirements (from ``uv.lock``, requirement
files, ``poetry.lock``, ``Pipfile.lock`` or static metadata: :mod:`coverset.lang.python_env`, called
by the language adapter) plus the pytest-coverset wheel, on one Python version. Its identity is
``env_hash = sha256(requirements.txt \\0 python \\0 sha256(wheel))``, so a lockfile change, a Python
bump or a plugin change each produce a new environment, and anything else reuses the cached one. The
requirements text also records what is installed from the checkout (``# coverset-local:`` lines and
the build backend when the project itself is installed), so those are part of the identity too;
:attr:`EnvSpec.env_source` and :attr:`EnvSpec.install` say where it came from and how the project
gets in.

The wheel is built once per plugin source tree: ``uv build --wheel packages/pytest-coverset`` into
``~/.cache/coverset/wheels/<source hash>/``, under an exclusive file lock and an atomic rename, and
memoised for the life of the process.

Both executors install the wheel with ``--no-deps`` (the LocalExecutor with ``uv pip install``, the
Modal image with ``pip install``), so its runtime dependencies come from the requirements:
:func:`build_env_spec` appends ``coverage==<the version in coverset's own uv.lock>`` (and
``pytest==...`` when the repo's lock has no pytest at all) unless the repo's lock already pins them,
which is respected. A target repo need not depend on ``coverage`` at all. Added pins are part of the
identity: ``env_hash`` becomes ``sha256(adapter env_hash \0 added pins)``.

:func:`direct_references` lists the requirement lines that would fetch code from somewhere other
than the package index by name and version (``pkg @ <url>``, VCS and ``file:`` references, pip
options such as ``--index-url`` or ``-e``). Building such an environment runs code from that
location with network access, so the runner never builds one for an untrusted pull request.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import importlib.metadata
import re
import shutil
import subprocess
import tempfile
import threading
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any

from coverset.executors.base import ExecutorError, cache_root
from coverset.lang.base import EnvSpec, LanguageAdapter

__all__ = [
    "PLUGIN_DIR",
    "build_env_spec",
    "build_plugin_wheel",
    "direct_references",
    "env_hash",
    "file_lock",
    "plugin_runtime_pins",
    "plugin_source_hash",
    "with_plugin_pins",
]

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "packages" / "pytest-coverset"
"""The pytest-coverset source tree in a coverset checkout."""

COVERSET_LOCK = PLUGIN_DIR.parents[1] / "uv.lock"
"""coverset's own lockfile: the versions of the plugin's runtime dependencies it is tested with."""
PLUGIN_RUNTIME_DEPS = ("coverage", "pytest")
_PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:===?|@)")
UV_BUILD_TIMEOUT_S = 300
_SKIP_PARTS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build", ".venv"})
_wheel_memo: dict[str, Path] = {}
_wheel_memo_lock = threading.Lock()


def env_hash(requirements_txt: str, python: str, wheel_sha256: str | None) -> str:
    """The environment identity (same formula as :meth:`PythonAdapter.env_spec`)."""
    material = "\0".join((requirements_txt, python, wheel_sha256 or "none"))
    return hashlib.sha256(material.encode()).hexdigest()


def plugin_source_hash(plugin_dir: Path = PLUGIN_DIR) -> str:
    """sha256 over the plugin's ``pyproject.toml`` and ``src/`` (paths and contents, sorted)."""
    digest = hashlib.sha256()
    files = [plugin_dir / "pyproject.toml", *sorted((plugin_dir / "src").rglob("*"))]
    for path in files:
        rel = path.relative_to(plugin_dir)
        if not path.is_file() or path.is_symlink() or _SKIP_PARTS.intersection(rel.parts):
            continue
        digest.update(rel.as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_plugin_wheel(
    plugin_dir: Path = PLUGIN_DIR, *, cache_dir: Path | None = None, uv: str | None = None
) -> Path:
    """Return the pytest-coverset wheel for the current plugin source, building it at most once.

    Raises :class:`ExecutorError` when the plugin sources are missing or ``uv build`` fails.
    """
    if not (plugin_dir / "pyproject.toml").is_file():
        raise ExecutorError(f"pytest-coverset sources not found at {plugin_dir}")
    source_hash = plugin_source_hash(plugin_dir)
    with _wheel_memo_lock:
        cached = _wheel_memo.get(source_hash)
        if cached is not None and cached.is_file():
            return cached
        wheels_dir = (cache_dir or cache_root()) / "wheels"
        wheels_dir.mkdir(parents=True, exist_ok=True)
        final = wheels_dir / source_hash[:32]
        with file_lock(wheels_dir / f"{final.name}.lock"):
            wheel = _find_wheel(final)
            if wheel is None:
                wheel = _build_into(plugin_dir, wheels_dir, final, uv)
        _wheel_memo[source_hash] = wheel
        return wheel


def build_env_spec(
    adapter: LanguageAdapter, checkout_dir: Path, cfg: Mapping[str, Any], plugin_wheel: Path | None
) -> EnvSpec:
    """The :class:`EnvSpec` of the checkout at ``checkout_dir`` with ``plugin_wheel`` installed.

    ``cfg`` may override ``python``; the adapter resolves the requirements (``uv export``, a lock
    conversion or ``uv pip compile``; see :mod:`coverset.lang.python_env`) and hashes the environment.
    """
    values = dict(cfg)
    if plugin_wheel is not None:
        values["plugin_wheel"] = str(plugin_wheel)
    spec = adapter.env_spec(checkout_dir, values)
    return with_plugin_pins(spec) if plugin_wheel is not None else spec


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned_packages(requirements_txt: str) -> set[str]:
    """Normalised names of the packages ``requirements_txt`` pins exactly (``==``/``===``/``@``)."""
    names = set()
    for line in requirements_txt.splitlines():
        match = _PIN_RE.match(line)
        if match:
            names.add(_normalise(match.group(1)))
    return names


_DIRECT_REF_RE = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*(?:\[[^\]]*\])?\s*@")
_VCS_MARKERS = ("git+", "hg+", "svn+", "bzr+", "file:", "://")


def direct_references(requirements_txt: str) -> list[str]:
    """Requirement lines that are not a plain ``name[extras]==version`` from the index (see the
    module docstring); each cut to 80 characters. Comments and blank lines are ignored."""
    found = []
    for raw in requirements_txt.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lowered = line.lower()
        if (
            line.startswith("-")
            or " @ " in line
            or _DIRECT_REF_RE.match(line)
            or any(marker in lowered for marker in _VCS_MARKERS)
        ):
            found.append(line[:80])
    return found


def listed_packages(requirements_txt: str) -> set[str]:
    """Normalised names of every package a requirements file mentions (pinned or not)."""
    names = set()
    for line in requirements_txt.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match and not line.lstrip().startswith(("#", "-")):
            names.add(_normalise(match.group(1)))
    return names


@cache
def plugin_runtime_pins(lock: Path = COVERSET_LOCK) -> dict[str, str]:
    """``{name: version}`` of the plugin's runtime dependencies in coverset's ``uv.lock``.

    Falls back to the versions installed next to coverset when the lock is not there (an installed
    coverset rather than a checkout).
    """
    versions: dict[str, str] = {}
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8"))
        for package in data.get("package", []):
            name = _normalise(str(package.get("name", "")))
            if name in PLUGIN_RUNTIME_DEPS and isinstance(package.get("version"), str):
                versions[name] = package["version"]
    except (OSError, tomllib.TOMLDecodeError):
        pass
    for name in PLUGIN_RUNTIME_DEPS:
        if name not in versions:
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                continue
    return versions


def with_plugin_pins(spec: EnvSpec, pins: Mapping[str, str] | None = None) -> EnvSpec:
    """``spec`` with exact pins for the plugin's runtime dependencies the repo's lock lacks.

    ``coverage`` is pinned unless the requirements pin it already; ``pytest`` only when the
    requirements do not mention it at all (a repo's own pytest is never replaced). The added pins
    go into ``env_hash``.
    """
    versions = dict(pins if pins is not None else plugin_runtime_pins())
    pinned, listed = pinned_packages(spec.requirements_txt), listed_packages(spec.requirements_txt)
    added = []
    if "coverage" not in pinned and "coverage" in versions:
        added.append(f"coverage=={versions['coverage']}")
    if "pytest" not in listed and "pytest" in versions:
        added.append(f"pytest=={versions['pytest']}")
    if not added:
        return spec
    text = spec.requirements_txt
    if text and not text.endswith("\n"):
        text += "\n"
    text += "".join(f"{pin}\n" for pin in added)
    digest = hashlib.sha256("\0".join((spec.env_hash, *added)).encode()).hexdigest()
    return dataclasses.replace(spec, requirements_txt=text, env_hash=digest)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``path`` (created if missing) for a ``with`` block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _find_wheel(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    wheels = sorted(directory.glob("*.whl"))
    return wheels[0] if wheels else None


def _build_into(plugin_dir: Path, wheels_dir: Path, final: Path, uv: str | None) -> Path:
    uv_bin = uv or shutil.which("uv")
    if uv_bin is None:
        raise ExecutorError("uv is not installed or not on PATH (needed to build the pytest-coverset wheel)")
    staging = Path(tempfile.mkdtemp(dir=wheels_dir, prefix=".build-"))
    try:
        proc = subprocess.run(
            [uv_bin, "build", "--wheel", str(plugin_dir), "-o", str(staging), "--quiet"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=UV_BUILD_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()[-800:]
            raise ExecutorError(f"building the pytest-coverset wheel failed: {stderr}")
        if _find_wheel(staging) is None:
            raise ExecutorError("uv build produced no wheel")
        if final.exists():
            shutil.rmtree(final)
        staging.rename(final)
    except subprocess.TimeoutExpired as exc:
        raise ExecutorError(f"building the pytest-coverset wheel timed out: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    wheel = _find_wheel(final)
    if wheel is None:
        raise ExecutorError("the built wheel vanished")
    return wheel
