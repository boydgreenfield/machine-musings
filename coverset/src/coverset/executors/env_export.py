"""Environment export shared by every executor: the plugin wheel and the :class:`EnvSpec`.

An environment is the target repo's locked third-party requirements (``uv export``, done by the
language adapter) plus the pytest-coverset wheel, on one Python version. Its identity is
``env_hash = sha256(requirements.txt \\0 python \\0 sha256(wheel))``, so a lockfile change, a Python
bump or a plugin change each produce a new environment, and anything else reuses the cached one.

The wheel is built once per plugin source tree: ``uv build --wheel packages/pytest-coverset`` into
``~/.cache/coverset/wheels/<source hash>/``, under an exclusive file lock and an atomic rename, and
memoised for the life of the process.
"""

from __future__ import annotations

import fcntl
import hashlib
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from coverset.executors.base import ExecutorError, cache_root
from coverset.lang.base import EnvSpec, LanguageAdapter

__all__ = [
    "PLUGIN_DIR",
    "build_env_spec",
    "build_plugin_wheel",
    "env_hash",
    "file_lock",
    "plugin_source_hash",
]

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "packages" / "pytest-coverset"
"""The pytest-coverset source tree in a coverset checkout."""

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

    ``cfg`` may override ``python``; the adapter exports the requirements (``uv export``) and hashes
    the environment.
    """
    values = dict(cfg)
    if plugin_wheel is not None:
        values["plugin_wheel"] = str(plugin_wheel)
    return adapter.env_spec(checkout_dir, values)


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
