"""The Python language adapter: config, environment, pytest command, bundle and function index.

Per-repo configuration lives in the target repo's ``pyproject.toml`` under ``[tool.coverset]``;
every key has a default, and :meth:`PythonAdapter.load_config` infers ``source_root``, ``source``,
``testpaths`` and ``python`` from the checkout when they are not set. The configuration comes from
the repository under test, so it is validated strictly: paths must be relative and stay inside the
repo, module names must be dotted identifiers. Files are read from the checkout only when they are
regular files (never through a symlink, never a FIFO or device) and below a size cap, and error
messages never echo more than a short, sanitised fragment of what the repo supplied.

The environment is exported with ``uv export`` limited to third-party requirements: the project,
its workspace members and local path dependencies are never emitted, because the project is
never installed (tests import it through ``PYTHONPATH=<source_root>``). ``uv`` runs offline, ignores
``uv.toml`` and ``[tool.uv]`` settings, and sees a minimal environment with a throwaway ``HOME``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Final, Literal

from coverset.analysis.diff import FileDiff, FileStatus, changed_functions
from coverset.analysis.functions import index_functions
from coverset.analysis.shapes import Catalogue, annotation_index, enum_index
from coverset.facts.convert import read_python_bundle
from coverset.facts.untrusted import BundleLimits
from coverset.lang.base import EnvSpec, FunctionChange, FunctionRegion, NeutralBundle

__all__ = [
    "ConfigError",
    "CoversetConfig",
    "EnvSpecError",
    "PythonAdapter",
    "read_tool_config",
]

DEFAULT_PYTHON: Final = "3.13"
MIN_PYTHON: Final = (3, 12)
UV_EXPORT_ARGS: Final = (
    "export",
    "--frozen",
    "--offline",
    "--no-config",
    "--no-hashes",
    "--no-emit-project",
    "--no-emit-workspace",
    "--no-emit-local",
    "--all-groups",
)
MAX_PYPROJECT_BYTES: Final = 1024 * 1024
MAX_PYTHON_VERSION_BYTES: Final = 64 * 1024
MAX_LOCK_BYTES: Final = 64 * 1024 * 1024
UV_EXPORT_TIMEOUT_S: Final = 120
_MODULE_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_PYTHON_RE: Final = re.compile(r"^(\d{1,4})\.(\d{1,4})(\.\d{1,4})?$", re.ASCII)
_NOT_SOURCE_PACKAGES: Final = frozenset({"tests", "test", "docs", "examples", "scripts", "benchmarks"})
_SAFE_CHARS_RE: Final = re.compile(r"[^A-Za-z0-9._+-]")

type FailFast = Literal["finish_in_background", "cancel_remaining"]


class ConfigError(ValueError):
    """``[tool.coverset]`` is malformed or unsafe."""


class EnvSpecError(RuntimeError):
    """The test environment cannot be described (no ``uv.lock``, ``uv export`` failed)."""


@dataclass(frozen=True, slots=True)
class CoversetConfig:
    """``[tool.coverset]`` with defaults applied."""

    source: tuple[str, ...] = ()
    source_root: str = "."
    testpaths: tuple[str, ...] = ("tests",)
    python: str = DEFAULT_PYTHON
    max_shards: int = 16
    target_shard_seconds: float = 20.0
    fail_fast: FailFast = "finish_in_background"
    invariants: str = "invariants.yaml"
    trace_modules: tuple[str, ...] = ()
    tracked_globs: tuple[str, ...] = ("uv.lock", "conftest.py")
    reorder: bool = True
    unknown_keys: tuple[str, ...] = ()
    """Keys present in ``[tool.coverset]`` that this version does not know (ignored)."""


def read_tool_config(pyproject_text: str, *, repo_root: Path | None = None) -> CoversetConfig:
    """Parse ``[tool.coverset]`` from ``pyproject_text``; with ``repo_root``, infer unset keys.

    Without a checkout the defaults are static: ``source=()``, ``source_root="."``, ``testpaths``
    from ``[tool.pytest.ini_options]`` else ``("tests",)``, ``python="3.13"``.
    """
    try:
        doc = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"pyproject.toml does not parse: {exc}") from exc
    tool = doc.get("tool", {})
    raw = tool.get("coverset", {}) if isinstance(tool, dict) else {}
    if not isinstance(raw, dict):
        raise ConfigError("[tool.coverset] must be a table")
    pytest_ini = tool.get("pytest", {}).get("ini_options", {}) if isinstance(tool.get("pytest"), dict) else {}
    known = set(CoversetConfig.__dataclass_fields__) - {"unknown_keys"}
    values: dict[str, Any] = {"unknown_keys": tuple(sorted(set(raw) - known))}

    source_root = _rel_path(raw["source_root"], "source_root") if "source_root" in raw else None
    if source_root is None:
        source_root = "src" if repo_root is not None and _is_real_dir(repo_root, "src") else "."
    values["source_root"] = source_root
    if "source" in raw:
        values["source"] = _modules(raw["source"], "source")
    elif repo_root is not None:
        values["source"] = _infer_packages(repo_root, source_root)
    if "testpaths" in raw:
        values["testpaths"] = tuple(_argv_path(p) for p in _str_list(raw["testpaths"], "testpaths"))
    elif isinstance(pytest_ini, dict) and "testpaths" in pytest_ini:
        values["testpaths"] = tuple(_argv_path(p) for p in _str_list(pytest_ini["testpaths"], "testpaths"))
    if "python" in raw:
        values["python"] = _python_version(raw["python"])
    elif repo_root is not None and (pinned := _pinned_python(repo_root)) is not None:
        values["python"] = pinned
    if "max_shards" in raw:
        values["max_shards"] = _positive_int(raw["max_shards"], "max_shards")
    if "target_shard_seconds" in raw:
        seconds = raw["target_shard_seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not seconds > 0:
            raise ConfigError("target_shard_seconds must be a positive number")
        values["target_shard_seconds"] = float(seconds)
    if "fail_fast" in raw:
        if raw["fail_fast"] not in ("finish_in_background", "cancel_remaining"):
            raise ConfigError("fail_fast must be 'finish_in_background' or 'cancel_remaining'")
        values["fail_fast"] = raw["fail_fast"]
    if "invariants" in raw:
        values["invariants"] = _rel_path(raw["invariants"], "invariants")
    if "trace_modules" in raw:
        values["trace_modules"] = _modules(raw["trace_modules"], "trace_modules")
    if "tracked_globs" in raw:
        values["tracked_globs"] = tuple(
            _rel_path(g, "tracked_globs") for g in _str_list(raw["tracked_globs"], "tracked_globs")
        )
    if "reorder" in raw:
        if not isinstance(raw["reorder"], bool):
            raise ConfigError("reorder must be true or false")
        values["reorder"] = raw["reorder"]
    return CoversetConfig(**values)


class PythonAdapter:
    """:class:`~coverset.lang.base.LanguageAdapter` for pytest projects locked with uv."""

    name = "python"

    def __init__(self, *, uv: str | None = None, export_timeout_s: float = UV_EXPORT_TIMEOUT_S) -> None:
        self._uv = uv
        self._export_timeout_s = export_timeout_s

    def detect(self, repo_root: Path) -> bool:
        """True for a repo with ``pyproject.toml``, ``setup.cfg`` or ``setup.py``, or top-level ``*.py``."""
        if any((repo_root / name).is_file() for name in ("pyproject.toml", "setup.cfg", "setup.py")):
            return True
        return any(path.is_file() for path in repo_root.glob("*.py"))

    def load_config(self, repo_root: Path) -> CoversetConfig:
        """``[tool.coverset]`` of the repo at ``repo_root`` with inferred defaults."""
        text = _read_repo_file(repo_root, "pyproject.toml", MAX_PYPROJECT_BYTES) or ""
        return read_tool_config(text, repo_root=repo_root)

    def env_spec(self, repo_root: Path, cfg: Mapping) -> EnvSpec:
        """Export the locked requirements and hash the environment.

        ``cfg`` may override ``python`` and name the pytest-coverset wheel as ``plugin_wheel``.
        ``env_hash = sha256(requirements.txt, python version, plugin wheel sha256 or "none")``.
        """
        config = self.load_config(repo_root)
        python = _python_version(cfg.get("python") or config.python)
        try:
            lock_hash = _sha256_file(repo_root / "uv.lock", max_bytes=MAX_LOCK_BYTES, no_follow=True)
        except FileNotFoundError:
            raise EnvSpecError(
                f"{repo_root} has no uv.lock: coverset needs a uv-locked project (run `uv lock`)"
            ) from None
        except ConfigError as exc:
            raise EnvSpecError(str(exc)) from exc
        requirements = self._uv_export(repo_root)
        wheel = Path(cfg["plugin_wheel"]) if cfg.get("plugin_wheel") else None
        wheel_hash = _sha256_file(wheel) if wheel is not None else "none"
        env_hash = hashlib.sha256("\0".join((requirements, python, wheel_hash)).encode()).hexdigest()
        return EnvSpec(
            python=python,
            lock_hash=lock_hash,
            requirements_txt=requirements,
            plugin_wheel=wheel,
            source_root=config.source_root,
            env_hash=env_hash,
        )

    def test_command(
        self, args_file: str, scores_file: str | None, out_dir: str, extra: Mapping
    ) -> list[str]:
        """The pytest argv for one shard.

        ``extra`` keys (all optional): ``python`` (interpreter, default ``python``), ``sources``,
        ``trace_modules``, ``full_trace_file``, ``shapes`` (``on``/``off``), ``repr_examples``,
        ``stop_file``, and the booleans ``xdist`` / ``pytest_cov`` saying whether those plugins
        are installed in the target environment (then ``--numprocesses=0`` / ``--no-cov`` are added).

        Every option that takes a value is emitted as one word, ``--opt=value``. pytest looks for its
        configuration file (``_initini``) before entry-point plugins have registered their options, so
        a separate value word (``--coverset-out run/out``) is taken for a positional path: the paths
        under the worker's sibling ``run/`` directory would move the inferred rootdir to their common
        ancestor with the repo, no ``pyproject.toml``/``pytest.ini`` would be found, and the target
        repo's ``xfail_strict``, ``addopts``, ``markers``, ``pythonpath``, ``filterwarnings`` and
        plugin settings would be silently dropped in every shard.
        """
        cmd = [
            str(extra.get("python") or "python"),
            "-m",
            "pytest",
            "@" + args_file,
            f"--coverset-out={out_dir}",
        ]
        if scores_file:
            cmd.append(f"--coverset-scores={scores_file}")
        cmd += [f"--coverset-source={source}" for source in extra.get("sources", ())]
        cmd += [f"--coverset-trace-module={module}" for module in extra.get("trace_modules", ())]
        options = {
            "full_trace_file": "--coverset-full-trace",
            "shapes": "--coverset-shapes",
            "repr_examples": "--coverset-repr-examples",
            "stop_file": "--coverset-stop-file",
        }
        for key, flag in options.items():
            if extra.get(key):
                cmd.append(f"{flag}={extra[key]}")
        cmd += ["--continue-on-collection-errors", "-p", "no:cacheprovider", "-q"]
        if extra.get("xdist"):
            cmd.append("--numprocesses=0")
        if extra.get("pytest_cov"):
            cmd.append("--no-cov")
        return cmd

    def read_bundle(
        self,
        bundle_dir: Path,
        *,
        limits: BundleLimits | None = None,
        source_root: Path | None = None,
        head_paths: Collection[str] | None = None,
    ) -> NeutralBundle:
        """Convert an extracted pytest-coverset bundle (see :mod:`coverset.facts.convert`).

        ``limits`` are the untrusted-bundle caps, ``source_root`` a checkout of the head commit
        (for ``n_lines``) and ``head_paths`` the head tree's paths (any other path is dropped);
        all three are passed to :func:`~coverset.facts.convert.read_python_bundle` unchanged.
        """
        return read_python_bundle(bundle_dir, limits=limits, source_root=source_root, head_paths=head_paths)

    def changed_functions(
        self, path: str, old_src: str | None, new_src: str | None, added: set[int], removed: set[int]
    ) -> list[FunctionChange]:
        """Classify the regions of one file (renames are detected within the file only)."""
        status: FileStatus = "added" if old_src is None else "deleted" if new_src is None else "modified"
        diff = FileDiff(path, status, frozenset(added), frozenset(removed))
        return changed_functions(self, {path: old_src}, {path: new_src}, {path: diff})

    def index_functions(self, path: str, source: str) -> list[FunctionRegion]:
        """Function, class and module regions of one Python file."""
        return index_functions(path, source)

    def shape_catalogue(self, sources: Mapping[str, str] | None = None) -> Catalogue:
        """The input-class catalogue for Python argument shapes.

        ``sources`` (path → text of the head commit's measured files) supplies parameter
        annotations, so the catalogue ``facts.ingest.finalize_run`` calls per function applies the
        same Union/Literal/Optional/bool rules as the UI, and the ``Enum`` classes defined in those
        files (:func:`~coverset.analysis.shapes.enum_index`) as the ``enum_members`` resolver.
        """
        if not sources:
            return Catalogue()
        enums = enum_index(sources)
        return Catalogue(enums.get if enums else None, annotations=annotation_index(sources))

    def _uv_export(self, repo_root: Path) -> str:
        uv = self._uv or shutil.which("uv")
        if uv is None:
            raise EnvSpecError("uv is not installed or not on PATH")
        with tempfile.TemporaryDirectory(prefix="coverset-uv-") as home:
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": home,
                "UV_CACHE_DIR": os.path.join(home, "cache"),
                "UV_NO_CONFIG": "1",
                "UV_OFFLINE": "1",
                "UV_PYTHON_DOWNLOADS": "never",
                "LANG": "C.UTF-8",
            }
            try:
                proc = subprocess.run(
                    [uv, *UV_EXPORT_ARGS],
                    cwd=repo_root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=self._export_timeout_s,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise EnvSpecError(f"uv export failed: {exc}") from exc
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()[-800:]
            raise EnvSpecError(f"uv export failed in {repo_root}: {stderr}")
        return proc.stdout.decode("utf-8")


def _sha256_file(path: Path, *, max_bytes: int | None = None, no_follow: bool = False) -> str:
    """sha256 of ``path``; with ``no_follow`` it must be a regular file, not a symlink."""
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path, no_follow=no_follow) as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise ConfigError(f"{path.name} is larger than {max_bytes} bytes")
            digest.update(chunk)
    return digest.hexdigest()


def _open_regular(path: Path, *, no_follow: bool) -> BinaryIO:
    """Open ``path`` for reading; with ``no_follow`` refuse symlinks, FIFOs and devices.

    ``O_NONBLOCK`` keeps a FIFO from blocking the open; it is rejected right after by ``fstat``.
    Raises FileNotFoundError when the file does not exist.
    """
    if not no_follow:
        return path.open("rb")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP for a symlink under O_NOFOLLOW
        raise ConfigError(f"{path.name} must be a regular file in the repository") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ConfigError(f"{path.name} must be a regular file in the repository")
    os.set_blocking(fd, True)
    return os.fdopen(fd, "rb")


def _read_repo_file(repo_root: Path, name: str, max_bytes: int) -> str | None:
    """Text of the regular file ``name`` at the top of the checkout, or None when absent.

    Symlinks, FIFOs, devices, files over ``max_bytes`` and non-UTF-8 text raise ConfigError.
    """
    path = repo_root / name
    try:
        fh = _open_regular(path, no_follow=True)
    except FileNotFoundError:
        return None
    with fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ConfigError(f"{name} is larger than {max_bytes} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{name} is not UTF-8 text") from exc


def _pinned_python(repo_root: Path) -> str | None:
    """The major.minor version pinned by ``.python-version``, or None when it is absent or empty."""
    text = _read_repo_file(repo_root, ".python-version", MAX_PYTHON_VERSION_BYTES)
    words = (text or "").split()
    if not words:
        return None
    match = _PYTHON_RE.fullmatch(words[0])
    if match is None:
        raise ConfigError(".python-version must start with a CPython version such as 3.13")
    return _python_version(words[0])


def _is_real_dir(repo_root: Path, rel: str) -> bool:
    """Whether ``rel`` is a directory inside the checkout reached without leaving it."""
    path = repo_root / rel
    try:
        return path.is_dir() and path.resolve().is_relative_to(repo_root.resolve())
    except OSError:
        return False


def _infer_packages(repo_root: Path, source_root: str) -> tuple[str, ...]:
    """Top-level importable packages under ``source_root`` (directories with ``__init__.py``)."""
    if not _is_real_dir(repo_root, source_root):
        return ()
    root = repo_root / source_root
    found = [
        child.name
        for child in root.iterdir()
        if not child.is_symlink()
        and child.is_dir()
        and (child / "__init__.py").is_file()
        and _MODULE_RE.fullmatch(child.name)
        and child.name not in _NOT_SOURCE_PACKAGES
    ]
    return tuple(sorted(found))


def _str_list(value: object, key: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{key} must be a string or a list of strings")
    return value


def _modules(value: object, key: str) -> tuple[str, ...]:
    modules = _str_list(value, key)
    for module in modules:
        # The pattern also keeps out words starting with "-" or "@": these are spliced into argv
        # (--coverset-source=..., --coverset-trace-module=...).
        if not _MODULE_RE.fullmatch(module):
            raise ConfigError(f"{key}: {_shown(module)} is not a dotted module name")
    return tuple(modules)


def _rel_path(value: object, key: str) -> str:
    """A repo-relative POSIX path that does not escape the repo."""
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ConfigError(f"{key} must be a relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{key} must stay inside the repository: {_shown(value)}")
    return str(path)


def _argv_path(value: object, key: str = "testpaths") -> str:
    """A repo-relative path that is spliced into pytest's argv as a word of its own.

    It must not start with ``-`` (pytest would take it for an option, e.g. ``-p evil``) or ``@``
    (argparse would read further arguments from that file).
    """
    path = _rel_path(value, key)
    if path.startswith(("-", "@")):
        raise ConfigError(f"{key} entries must not start with '-' or '@': {_shown(value)}")
    return path


def _positive_int(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{key} must be a positive integer")
    return value


def _python_version(value: object) -> str:
    match = _PYTHON_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ConfigError(f"python must look like '3.13', got {_shown(value)}")
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) < MIN_PYTHON:
        raise ConfigError(f"instrumented repos need Python 3.12+, got {_shown(value)}")
    return f"{major}.{minor}"  # "03.13" and "3.013" both mean 3.13


def _shown(value: object) -> str:
    """A short, sanitised rendering of a repo-supplied value for error messages."""
    text = _SAFE_CHARS_RE.sub("?", str(value)[:20])
    return repr(text + ("..." if len(str(value)) > 20 else ""))
