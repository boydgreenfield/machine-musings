"""The Python language adapter: config, environment, pytest command, bundle and function index.

Per-repo configuration lives in the target repo's ``pyproject.toml`` under ``[tool.coverset]``;
every key has a default, and :func:`explain_config` infers ``source_root``, ``source``,
``testpaths`` and ``python`` from the checkout when they are not set, recording how each was
inferred (``coverset doctor`` prints it). The configuration comes from the repository under test, so
it is validated strictly: paths must be relative and stay inside the repo, module names must be
dotted identifiers, ``pytest_args`` may only be options, and the ones known to conflict with how
coverset runs pytest (writing outside the worker, choosing tests by name or path, stopping a shard
early, coverset's own flags) are refused. That list is a guard against mistakes, not a sandbox: the
repository's ``conftest.py`` runs in the worker anyway. Files are read from the checkout only when they are regular files (never
through a symlink, never a FIFO or device) and below a size cap, and error messages never echo more
than a short, sanitised fragment of what the repo supplied.

Inference, in order of precedence:

* ``source_root``: the key; the package directory the build configuration names
  (``[tool.setuptools] package-dir``, ``packages.find.where``, hatch's wheel ``packages``, Poetry's
  ``from``, ``setup.cfg``/``setup.py`` ``package_dir``); pytest's ``pythonpath``; ``src`` when it
  holds code; the directory above the package that holds the tests (``flask/app/tests`` gives
  ``flask``); a type checker's root (ty, pyright, mypy); else ``.``.
* ``source``: the key; packages the build configuration names that exist under the root; regular
  packages (directories with ``__init__.py``) and, in a ``src`` layout, top-level modules; namespace
  packages (PEP 420 directories of ``.py`` files) when there is nothing else; for a flat project with
  no package, the module named like the project or its ``py-modules``.
* ``testpaths``: the key; ``testpaths`` from pytest's own configuration file (the one pytest would
  pick: ``pytest.toml``, ``pytest.ini``, ``pyproject.toml``, ``tox.ini``, ``setup.cfg``); the existing
  of ``tests``, ``test`` and ``testing``; else nothing, which lets pytest collect from the rootdir.
* ``python``: the key; ``.python-version``; the first of 3.13, 3.12 and 3.14 that
  ``requires-python`` allows; 3.13.
* ``tracked_globs``: the key; ``uv.lock`` and ``conftest.py`` plus the files the environment is read
  from when there is no ``uv.lock`` (``poetry.lock``, ``Pipfile.lock``, the requirement files).

The environment comes from :mod:`coverset.lang.python_env` (``uv.lock``, requirement files,
``poetry.lock``, ``Pipfile.lock``, ``[project]`` metadata or ``setup.cfg``/``setup.py`` literals); the
project itself is imported through ``PYTHONPATH=<source_root>`` and installed only when
``[tool.coverset] install`` (or its ``auto`` detection) says so.
"""

from __future__ import annotations

import configparser
import hashlib
import re
import shlex
import tomllib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from coverset.analysis.diff import FileDiff, FileStatus, changed_functions
from coverset.analysis.functions import index_functions
from coverset.analysis.shapes import Catalogue, annotation_index, enum_index
from coverset.facts.convert import read_python_bundle
from coverset.facts.untrusted import BundleLimits
from coverset.lang.base import EnvSpec, FunctionChange, FunctionRegion, NeutralBundle
from coverset.lang.python_env import (
    INSTALL_SETTINGS,
    MAX_LOCK_BYTES,
    MAX_PYPROJECT_BYTES,
    UV_EXPORT_ARGS,
    ConfigError,
    EnvSource,
    EnvSpecError,
    InstallPlan,
    InstallSetting,
    ProjectMetadata,
    ResolvedEnv,
    detect_env_source,
    project_metadata,
    read_repo_text,
    rel_path,
    resolve_environment,
    resolve_install,
    sha256_file,
    shown,
)

__all__ = [
    "ConfigError",
    "CoversetConfig",
    "EnvReport",
    "EnvSpecError",
    "MAX_LOCK_BYTES",
    "MAX_PYPROJECT_BYTES",
    "PytestConfig",
    "PythonAdapter",
    "UV_EXPORT_ARGS",
    "addopts_conflicts",
    "explain_config",
    "pytest_config",
    "read_tool_config",
    "suggested_addopts",
]

DEFAULT_PYTHON: Final = "3.13"
MIN_PYTHON: Final = (3, 12)
SUPPORTED_PYTHONS: Final = ("3.13", "3.12", "3.14")
"""Versions ``requires-python`` inference may pick, in order of preference."""
MAX_PYTHON_VERSION_BYTES: Final = 64 * 1024
MAX_INI_BYTES: Final = 1024 * 1024
UV_EXPORT_TIMEOUT_S: Final = 120
TEST_DIRS: Final = ("tests", "test", "testing")
_MODULE_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_PYTHON_RE: Final = re.compile(r"^(\d{1,4})\.(\d{1,4})(\.\d{1,4})?$", re.ASCII)
_NOT_SOURCE_PACKAGES: Final = frozenset(
    {"tests", "test", "testing", "docs", "doc", "examples", "scripts", "benchmarks", "build", "dist"}
)
_NOT_SOURCE_MODULES: Final = frozenset(
    {"setup", "conftest", "noxfile", "fabfile", "tasks", "manage", "__init__"}
)
_DERIVED_FIELDS: Final = frozenset({"unknown_keys"})

_PYTEST_DENIED_LONG: Final = (
    "--basetemp",
    "--rootdir",
    "--config-file",
    "--inifile",
    "--confcutdir",
    "--numprocesses",
    "--dist",
    "--maxprocesses",
    "--cov",
    "--no-cov",
    "--coverset",
    "--exitfirst",
    "--maxfail",
    "--last-failed",
    "--lf",
    "--failed-first",
    "--ff",
    "--new-first",
    "--nf",
    "--stepwise",
    "--sw",
    "--cache-clear",
    "--cache-show",
    "--collect-only",
    "--co",
    "--continue-on-collection-errors",
    "--pdb",
    "--trace",
    "--junitxml",
    "--junit-xml",
    "--resultlog",
    "--result-log",
    "--debug",
    "--pastebin",
    "--help",
    "--version",
    "--fixtures",
    "--markers",
    "--setup-plan",
    "--setup-only",
    "--keyword",
    "--quiet",
    "--verbose",
    "--verbosity",
    "--log-file",
    "--report-log",
    "--deselect",
    "--ignore",
    "--pyargs",
)
"""Long options ``pytest_args`` may not carry (prefix match): they write outside the worker, choose
tests by name or path (coverset plans the shards), stop a shard early, need the cache plugin coverset
disables, or are coverset's own."""
_OVERRIDE_INI_DENIED: Final = frozenset(
    {
        "log_file",
        "cache_dir",
        "python_files",
        "python_classes",
        "python_functions",
        "testpaths",
        "norecursedirs",
        "pythonpath",
        "collect_imported_tests",
    }
)
"""ini keys ``-o``/``--override-ini`` may not set: they write files or change what is collected or
imported. ``verbosity*`` keys are refused as well (they change the collect listing), and an
``addopts`` override is validated word by word like ``pytest_args`` itself."""
_PYTEST_DENIED_SHORT: Final = frozenset("cnxkhVqv")
"""Short flags refused anywhere in a combined word (``-vx``): ``-q``/``-v`` change the collect listing."""
_PYTEST_VALUE_SHORT: Final = frozenset({"-m", "-W", "-o", "-p", "-r"})
_CACHE_OPTIONS: Final = (
    "--lf",
    "--last-failed",
    "--ff",
    "--failed-first",
    "--nf",
    "--new-first",
    "--sw",
    "--stepwise",
    "--sw-skip",
    "--stepwise-skip",
    "--cache-show",
    "--cache-clear",
)

type FailFast = Literal["finish_in_background", "cancel_remaining"]


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
    requirements: tuple[str, ...] = ()
    """Requirement files that define the environment (overrides every lock and auto-detection)."""
    install: InstallSetting = "auto"
    """How the project itself is installed: ``auto``, ``none``, ``editable`` or ``wheel``."""
    pytest_args: tuple[str, ...] = ()
    """Extra pytest options for every collect and shard (validated: see :func:`_pytest_args`)."""
    unknown_keys: tuple[str, ...] = ()
    """Keys present in ``[tool.coverset]`` that this version does not know (ignored)."""


def read_tool_config(pyproject_text: str, *, repo_root: Path | None = None) -> CoversetConfig:
    """Parse ``[tool.coverset]`` from ``pyproject_text``; with ``repo_root``, infer unset keys.

    Without a checkout the defaults are static: ``source=()``, ``source_root="."``, ``testpaths``
    from ``[tool.pytest.ini_options]`` (or pytest 9's ``[tool.pytest]``) else ``("tests",)``,
    ``python="3.13"``.
    """
    return explain_config(pyproject_text, repo_root=repo_root)[0]


def explain_config(
    pyproject_text: str, *, repo_root: Path | None = None
) -> tuple[CoversetConfig, dict[str, str]]:
    """:func:`read_tool_config` plus, for each inferred key, how it was decided (for the doctor)."""
    try:
        doc = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"pyproject.toml does not parse: {exc}") from exc
    tool = doc.get("tool", {})
    raw = tool.get("coverset", {}) if isinstance(tool, dict) else {}
    if not isinstance(raw, dict):
        raise ConfigError("[tool.coverset] must be a table")
    known = set(CoversetConfig.__dataclass_fields__) - _DERIVED_FIELDS
    values: dict[str, Any] = {"unknown_keys": tuple(sorted(set(raw) - known))}
    how: dict[str, str] = {}
    meta = _metadata(repo_root, doc) if repo_root is not None else ProjectMetadata()
    ini = _pyproject_pytest(doc)
    if repo_root is not None:
        try:
            ini = pytest_config(repo_root, doc)
        except ConfigError:  # an unparsable tox.ini/setup.cfg only costs the inference (the doctor says so)
            ini = PytestConfig()

    if "source_root" in raw:
        source_root, how["source_root"] = _rel_path(raw["source_root"], "source_root"), "[tool.coverset]"
    elif repo_root is not None:
        source_root, how["source_root"] = _infer_source_root(repo_root, meta, doc, ini)
    else:
        source_root, how["source_root"] = ".", "default"
    values["source_root"] = source_root
    if "source" in raw:
        values["source"], how["source"] = _modules(raw["source"], "source"), "[tool.coverset]"
    elif repo_root is not None:
        values["source"], how["source"] = _infer_sources(repo_root, source_root, meta)
    if "testpaths" in raw:
        values["testpaths"] = tuple(_argv_path(p) for p in _str_list(raw["testpaths"], "testpaths"))
        how["testpaths"] = "[tool.coverset]"
    else:
        if ini.testpaths is not None:
            paths = tuple(_argv_path(p) for p in ini.testpaths)
            how["testpaths"] = f"testpaths in {ini.file}"
            if repo_root is not None:  # pytest skips testpaths that match nothing; so must the argv
                existing = _existing_testpaths(repo_root, paths)
                if existing != paths:
                    dropped = sorted(set(paths) - set(existing))
                    how["testpaths"] += " (missing, left out: " + ", ".join(dropped) + ")"
                paths = existing
            values["testpaths"] = paths
        elif repo_root is not None:
            found = tuple(d for d in TEST_DIRS if _is_real_dir(repo_root, d))
            values["testpaths"] = found
            how["testpaths"] = (
                "existing test directories" if found else "none found: pytest collects from the rootdir"
            )
    if "python" in raw:
        values["python"], how["python"] = _python_version(raw["python"]), "[tool.coverset]"
    elif repo_root is not None and (pinned := _pinned_python(repo_root)) is not None:
        values["python"], how["python"] = pinned, ".python-version"
    elif repo_root is not None and meta.requires_python:
        chosen = _python_for(meta.requires_python)
        if chosen is not None:
            values["python"], how["python"] = chosen, f"requires-python {meta.requires_python}"
        else:
            how["python"] = f"default (requires-python {meta.requires_python} allows no 3.12+ version)"
    else:
        how["python"] = "default"
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
        how["tracked_globs"] = "[tool.coverset]"
    elif repo_root is not None:
        values["tracked_globs"], how["tracked_globs"] = _infer_tracked(repo_root, raw)
    if "reorder" in raw:
        if not isinstance(raw["reorder"], bool):
            raise ConfigError("reorder must be true or false")
        values["reorder"] = raw["reorder"]
    if "requirements" in raw:
        values["requirements"] = tuple(
            _rel_path(p, "requirements") for p in _str_list(raw["requirements"], "requirements")
        )
    if "install" in raw:
        if raw["install"] not in INSTALL_SETTINGS:
            raise ConfigError("install must be 'auto', 'none', 'editable' or 'wheel'")
        values["install"] = raw["install"]
    if "pytest_args" in raw:
        values["pytest_args"] = _pytest_args(raw["pytest_args"])
    return CoversetConfig(**values), how


def _metadata(repo_root: Path, doc: Mapping[str, Any]) -> ProjectMetadata:
    """Project metadata for layout inference; unreadable metadata only costs the inference."""
    try:
        return project_metadata(repo_root, doc)
    except (ConfigError, OSError):
        return ProjectMetadata()


# -- pytest's own configuration ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PytestConfig:
    """The pytest configuration file pytest itself would use at the repository root."""

    file: str | None = None
    testpaths: tuple[str, ...] | None = None
    addopts: tuple[str, ...] = ()
    options: Mapping[str, object] = field(default_factory=dict)


def _pyproject_pytest(doc: Mapping[str, Any]) -> PytestConfig:
    tool = doc.get("tool") if isinstance(doc.get("tool"), dict) else {}
    table = tool.get("pytest") if isinstance(tool.get("pytest"), dict) else None
    if table is None:
        return PytestConfig()
    ini = table.get("ini_options") if isinstance(table.get("ini_options"), dict) else None
    options = ini if ini is not None else {k: v for k, v in table.items() if k != "ini_options"}
    name = "pyproject.toml [tool.pytest.ini_options]" if ini is not None else "pyproject.toml [tool.pytest]"
    return _pytest_from(name, options)


def _pytest_from(name: str, options: Mapping[str, object]) -> PytestConfig:
    testpaths = options.get("testpaths")
    paths = tuple(_str_list(testpaths, "testpaths")) if testpaths is not None else None
    if isinstance(testpaths, str):
        paths = tuple(testpaths.split())
    addopts = options.get("addopts", "")
    words = tuple(_split_opts(addopts))
    return PytestConfig(file=name, testpaths=paths, addopts=words, options=dict(options))


def _split_opts(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()
    return []


def pytest_config(repo_root: Path, doc: Mapping[str, Any] | None = None) -> PytestConfig:
    """pytest's configuration at ``repo_root``, found as pytest finds it (first match wins).

    ``pytest.toml``/``.pytest.toml`` (table ``[pytest]``), ``pytest.ini``/``.pytest.ini`` (always used,
    even without a ``[pytest]`` section), ``pyproject.toml`` with ``[tool.pytest]``, ``tox.ini`` with
    ``[pytest]``, ``setup.cfg`` with ``[tool:pytest]``. coverset runs pytest with
    ``--rootdir=<repo>``, so the file is looked up at the repository root only.
    """
    for name in ("pytest.toml", ".pytest.toml"):
        text = _ini_text(repo_root, name)
        if text is not None:
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{name} does not parse: {exc}") from exc
            table = data.get("pytest") if isinstance(data.get("pytest"), dict) else {}
            return _pytest_from(name, table)
    for name in ("pytest.ini", ".pytest.ini"):
        text = _ini_text(repo_root, name)
        if text is not None:
            return _pytest_from(name, _ini_section(text, name, "pytest") or {})
    from_pyproject = _pyproject_pytest(doc if doc is not None else _load_doc(repo_root))
    if from_pyproject.file is not None:
        return from_pyproject
    for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        text = _ini_text(repo_root, name)
        options = _ini_section(text, name, section) if text is not None else None
        if options is not None:
            return _pytest_from(name, options)
    return PytestConfig()


def _ini_text(repo_root: Path, name: str) -> str | None:
    return read_repo_text(repo_root, name, MAX_INI_BYTES)


def _ini_section(text: str, name: str, section: str) -> dict[str, object] | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise ConfigError(f"{name} does not parse: {str(exc)[:200]}") from exc
    if not parser.has_section(section):
        return None
    return dict(parser.items(section))


def _load_doc(repo_root: Path) -> dict[str, Any]:
    text = read_repo_text(repo_root, "pyproject.toml", MAX_PYPROJECT_BYTES)
    if not text:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"pyproject.toml does not parse: {exc}") from exc


def addopts_conflicts(addopts: tuple[str, ...], requirements_txt: str = "") -> list[tuple[str, str]]:
    """``(option, problem)`` for each ``addopts`` word that clashes with how coverset runs pytest.

    coverset adds ``-p no:cacheprovider`` (cache options become usage errors), deals tests into
    shards itself (``-x``/``--maxfail`` stop a shard at its first failure and the rest of it never
    runs; ``-n`` is neutralised with ``--numprocesses=0`` when pytest-xdist is installed, but is a
    usage error when it is not), and measures coverage itself (``--cov*`` is neutralised with
    ``--no-cov`` when pytest-cov is installed, a usage error when it is not).
    """
    lowered = requirements_txt.lower()
    has_xdist = "pytest-xdist" in lowered
    has_cov = "pytest-cov" in lowered
    problems: list[tuple[str, str]] = []
    for word in addopts:
        name = word.split("=", 1)[0]
        if name in _CACHE_OPTIONS:
            problems.append(
                (word, "needs the cache plugin, which coverset disables: pytest stops with a usage error")
            )
        elif name in ("-x", "--exitfirst") or name.startswith("--maxfail") or re.fullmatch(r"-x\w*", name):
            problems.append(
                (word, "stops a shard at its first failure; coverset's fail-fast already reports it")
            )
        elif name in ("-n", "--numprocesses") or re.fullmatch(r"-n\d+|-nauto", word):
            if not has_xdist:
                problems.append(
                    (word, "pytest-xdist is not in the environment: pytest stops with a usage error")
                )
        elif name.startswith("--cov") and not has_cov:
            problems.append((word, "pytest-cov is not in the environment: pytest stops with a usage error"))
    return problems


def suggested_addopts(addopts: tuple[str, ...], conflicts: list[tuple[str, str]]) -> str:
    """``addopts`` without the conflicting words, for ``pytest_args = ["--override-ini=addopts=..."]``."""
    bad = {word for word, _ in conflicts}
    kept: list[str] = []
    skip_next = False
    for index, word in enumerate(addopts):
        if skip_next:
            skip_next = False
            continue
        if word in bad:
            if word in ("-n", "--numprocesses", "--maxfail") and index + 1 < len(addopts):
                skip_next = True
            continue
        kept.append(word)
    return shlex.join(kept)


# -- the adapter --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EnvReport:
    """Everything :meth:`PythonAdapter.describe_env` decided, for ``coverset doctor``."""

    config: CoversetConfig
    python: str
    source: EnvSource
    resolved: ResolvedEnv
    install: InstallPlan
    spec: EnvSpec


class PythonAdapter:
    """:class:`~coverset.lang.base.LanguageAdapter` for pytest projects (see the module docstring)."""

    name = "python"

    def __init__(
        self,
        *,
        uv: str | None = None,
        export_timeout_s: float = UV_EXPORT_TIMEOUT_S,
        cache_dir: Path | None = None,
        offline_only: bool = False,
    ) -> None:
        self._uv = uv
        self._export_timeout_s = export_timeout_s
        self._cache_dir = cache_dir
        self._offline_only = offline_only

    def detect(self, repo_root: Path) -> bool:
        """True for a repo with ``pyproject.toml``, ``setup.cfg``, ``setup.py``, a ``Pipfile``, a
        ``requirements*.txt``, or top-level ``*.py``."""
        names = ("pyproject.toml", "setup.cfg", "setup.py", "Pipfile", "Pipfile.lock")
        if any((repo_root / name).is_file() for name in names):
            return True
        if any(path.is_file() for path in repo_root.glob("*requirements*.txt")):
            return True
        return any(path.is_file() for path in repo_root.glob("*.py"))

    def load_config(self, repo_root: Path) -> CoversetConfig:
        """``[tool.coverset]`` of the repo at ``repo_root`` with inferred defaults."""
        text = read_repo_text(repo_root, "pyproject.toml", MAX_PYPROJECT_BYTES) or ""
        return read_tool_config(text, repo_root=repo_root)

    def env_spec(self, repo_root: Path, cfg: Mapping) -> EnvSpec:
        """Resolve the requirements, decide the install mode and hash the environment.

        ``cfg`` may override ``python`` and name the pytest-coverset wheel as ``plugin_wheel``.
        ``env_hash = sha256(requirements.txt, python version, plugin wheel sha256 or "none")``; the
        requirements text of every source but ``uv.lock`` starts with a header naming the source
        and the digest of the resolved inputs, and an installed project adds its build requirements,
        so both are part of the identity.
        """
        return self.describe_env(repo_root, cfg).spec

    def describe_env(self, repo_root: Path, cfg: Mapping) -> EnvReport:
        """:meth:`env_spec` with every decision behind it (source, resolution, install plan)."""
        config = self.load_config(repo_root)
        python = _python_version(cfg.get("python") or config.python)
        try:
            doc = _load_doc(repo_root)
            source = detect_env_source(repo_root, requirements=config.requirements, pyproject=doc)
            plan = resolve_install(
                repo_root,
                config.install,
                packages=config.source,
                source_root=config.source_root,
                pyproject=doc,
            )
            resolved = resolve_environment(
                repo_root,
                source,
                python=python,
                uv=self._uv,
                cache_dir=self._cache_dir,
                timeout_s=self._export_timeout_s,
                offline_only=self._offline_only,
                pyproject=doc,
                extra_requirements=plan.build_requires if plan.mode != "none" else (),
            )
        except ConfigError as exc:
            raise EnvSpecError(str(exc)) from exc
        requirements = resolved.requirements_txt
        wheel = Path(cfg["plugin_wheel"]) if cfg.get("plugin_wheel") else None
        wheel_hash = sha256_file(wheel) if wheel is not None else "none"
        env_hash = hashlib.sha256("\0".join((requirements, python, wheel_hash)).encode()).hexdigest()
        spec = EnvSpec(
            python=python,
            lock_hash=resolved.inputs_hash,
            requirements_txt=requirements,
            plugin_wheel=wheel,
            source_root=config.source_root,
            env_hash=env_hash,
            env_source=source.kind,
            install=plan.mode,
        )
        return EnvReport(config, python, source, resolved, plan, spec)

    def test_command(
        self, args_file: str, scores_file: str | None, out_dir: str, extra: Mapping
    ) -> list[str]:
        """The pytest argv for one shard.

        ``extra`` keys (all optional): ``python`` (interpreter, default ``python``), ``sources``,
        ``trace_modules``, ``full_trace_file``, ``shapes`` (``on``/``off``), ``repr_examples``,
        ``stop_file``, the booleans ``xdist`` / ``pytest_cov`` saying whether those plugins
        are installed in the target environment (then ``--numprocesses=0`` / ``--no-cov`` are added),
        and ``pytest_args`` (``[tool.coverset] pytest_args``, already validated), appended last.

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
        cmd += list(extra.get("pytest_args", ()))
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


# -- inference helpers ----------------------------------------------------------------------------------


def _pinned_python(repo_root: Path) -> str | None:
    """The major.minor version pinned by ``.python-version``, or None when it is absent or empty."""
    text = read_repo_text(repo_root, ".python-version", MAX_PYTHON_VERSION_BYTES)
    words = (text or "").split()
    if not words:
        return None
    match = _PYTHON_RE.fullmatch(words[0])
    if match is None:
        raise ConfigError(".python-version must start with a CPython version such as 3.13")
    return _python_version(words[0])


def _python_for(requires_python: str) -> str | None:
    """The first of :data:`SUPPORTED_PYTHONS` that ``requires-python`` admits (any patch release)."""
    try:
        spec = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return None
    for candidate in SUPPORTED_PYTHONS:
        if spec.contains(f"{candidate}.99", prereleases=True) or spec.contains(
            f"{candidate}.0", prereleases=True
        ):
            return candidate
    return None


def _is_real_dir(repo_root: Path, rel: str) -> bool:
    """Whether ``rel`` is a directory inside the checkout reached without leaving it."""
    path = repo_root / rel
    try:
        return path.is_dir() and not path.is_symlink() and path.resolve().is_relative_to(repo_root.resolve())
    except OSError:
        return False


def _children(root: Path) -> list[Path]:
    try:
        return sorted(root.iterdir())
    except OSError:
        return []


def _regular_packages(root: Path) -> list[str]:
    return [
        child.name
        for child in _children(root)
        if not child.is_symlink()
        and child.is_dir()
        and (child / "__init__.py").is_file()
        and _MODULE_RE.fullmatch(child.name)
        and child.name not in _NOT_SOURCE_PACKAGES
    ]


def _modules_in(root: Path) -> list[str]:
    return [
        child.stem
        for child in _children(root)
        if child.suffix == ".py"
        and child.is_file()
        and not child.is_symlink()
        and _MODULE_RE.fullmatch(child.stem)
        and child.stem not in _NOT_SOURCE_MODULES
        and not child.stem.startswith(("test_", "_"))
    ]


def _namespace_packages(root: Path) -> list[str]:
    """Directories without ``__init__.py`` that hold Python files within two levels (PEP 420)."""
    found = []
    for child in _children(root):
        if (
            child.is_symlink()
            or not child.is_dir()
            or not _MODULE_RE.fullmatch(child.name)
            or child.name in _NOT_SOURCE_PACKAGES
            or (child / "__init__.py").exists()
        ):
            continue
        if any(child.glob("*.py")) or any(child.glob("*/*.py")):
            found.append(child.name)
    return found


def _infer_source_root(
    repo_root: Path, meta: ProjectMetadata, doc: Mapping[str, Any], ini: PytestConfig
) -> tuple[str, str]:
    """The directory to put on ``PYTHONPATH`` (see the module docstring for the order)."""

    def usable(rel: str) -> str | None:
        rel = rel.strip().removeprefix("./").rstrip("/") or "."
        try:
            rel = _rel_path(rel, "source_root")
        except ConfigError:
            return None
        if rel == "." or not _is_real_dir(repo_root, rel):
            return None
        return rel if _regular_packages(repo_root / rel) or _namespace_packages(repo_root / rel) else None

    if meta.package_dir and (found := usable(meta.package_dir)):
        return found, f"package directory in {meta.origin or 'the build configuration'}"
    for entry in _split_opts(ini.options.get("pythonpath", "")):
        if found := usable(entry):
            return found, f"pythonpath in {ini.file}"
    if _is_real_dir(repo_root, "src"):
        src = repo_root / "src"
        if _regular_packages(src) or _namespace_packages(src) or _modules_in(src):
            return "src", "src layout"
        if not _regular_packages(repo_root):
            return "src", "src layout"
    for testpath in ini.testpaths or ():
        if found := _package_parent(repo_root, testpath):
            return found, f"the package that holds the tests ({testpath} in {ini.file})"
    for hint, where in _checker_roots(doc):
        if found := usable(hint):
            return found, where
    if _is_real_dir(repo_root, "src"):
        return ".", "flat layout (src/ holds no Python packages)"
    return ".", "flat layout"


def _package_parent(repo_root: Path, testpath: str) -> str | None:
    """For tests inside a package (``flask/app/tests``), the directory that holds the top package."""
    parts = Path(testpath).parts
    for depth in range(1, len(parts)):
        parent, child = parts[:depth], parts[depth]
        if child in _NOT_SOURCE_PACKAGES or not _MODULE_RE.fullmatch(child):
            return None
        package = repo_root.joinpath(*parent, child)
        if (package / "__init__.py").is_file() and _is_real_dir(repo_root, "/".join((*parent, child))):
            return "/".join(parent)
    return None


def _checker_roots(doc: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Source roots that type checkers are told about: ty, pyright, mypy (first entries only)."""
    tool = doc.get("tool") if isinstance(doc.get("tool"), dict) else {}
    hints: list[tuple[str, str]] = []
    ty = tool.get("ty") if isinstance(tool.get("ty"), dict) else {}
    environment = ty.get("environment") if isinstance(ty.get("environment"), dict) else {}
    for entry in _split_opts(environment.get("root", [])):
        hints.append((entry, "[tool.ty.environment] root"))
    pyright = tool.get("pyright") if isinstance(tool.get("pyright"), dict) else {}
    for entry in _split_opts(pyright.get("extraPaths", [])):
        hints.append((entry, "[tool.pyright] extraPaths"))
    mypy = tool.get("mypy") if isinstance(tool.get("mypy"), dict) else {}
    for entry in _split_opts(mypy.get("mypy_path", "")):
        hints.append((entry, "[tool.mypy] mypy_path"))
    return hints


def _existing_testpaths(repo_root: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    """``paths`` that exist, glob patterns expanded (as pytest treats ini ``testpaths``)."""
    out: list[str] = []
    for path in paths:
        if any(ch in path for ch in "*?["):
            matches = sorted(
                str(m.relative_to(repo_root)) for m in repo_root.glob(path) if not m.is_symlink()
            )
            out += [m for m in matches if not m.startswith(("-", "@"))]
        elif (repo_root / path).exists():
            out.append(path)
    return tuple(dict.fromkeys(out))


def _infer_tracked(repo_root: Path, raw: Mapping[str, Any]) -> tuple[tuple[str, ...], str]:
    """``uv.lock`` and ``conftest.py`` plus the files the environment is read from."""
    from coverset.lang.python_env import auto_requirement_files

    extra: list[str] = []
    configured = raw.get("requirements")
    if isinstance(configured, list | str):
        extra += [p for p in _str_list(configured, "requirements")]
    elif not (repo_root / "uv.lock").is_file():
        extra += [n for n in ("poetry.lock", "Pipfile.lock") if (repo_root / n).is_file()]
        if not extra:
            extra += auto_requirement_files(repo_root)
    tracked = tuple(dict.fromkeys(("uv.lock", "conftest.py", *(_rel_path(p, "requirements") for p in extra))))
    return tracked, "default" if not extra else "default plus the environment's input files"


def _infer_sources(repo_root: Path, source_root: str, meta: ProjectMetadata) -> tuple[tuple[str, ...], str]:
    """The packages to measure under ``source_root`` and how they were found (see the module docstring)."""
    if not _is_real_dir(repo_root, source_root) and source_root not in ("", "."):
        return (), "source_root is not a directory in the repository"
    root = repo_root / source_root
    named = [
        name
        for name in meta.packages
        if _MODULE_RE.fullmatch(name) and ((root / name).is_dir() or (root / f"{name}.py").is_file())
    ]
    if named:
        return tuple(sorted(set(named))), f"packages named in {meta.origin or 'the build configuration'}"
    flat = source_root in ("", ".")
    regular = _regular_packages(root)
    modules = [] if flat else _modules_in(root)
    if regular or modules:
        what = "directories with __init__.py" + ("" if flat else " and modules")
        return tuple(sorted({*regular, *modules})), f"{what} under {source_root}"
    project = re.sub(r"[-.]+", "_", meta.name).lower() if meta.name else None
    namespaces = [n for n in _namespace_packages(root) if not flat or n == project]
    if namespaces:
        return tuple(sorted(namespaces)), f"namespace packages (no __init__.py) under {source_root}"
    if flat:
        singles = [m for m in meta.py_modules if (root / f"{m}.py").is_file() and _MODULE_RE.fullmatch(m)]
        if not singles and project and (root / f"{project}.py").is_file():
            singles = [project]
        if singles:
            return tuple(sorted(singles)), "single-module project"
    return (), "nothing found: set [tool.coverset] source"


def _infer_packages(repo_root: Path, source_root: str) -> tuple[str, ...]:
    """Top-level importable packages under ``source_root`` (kept for callers of the old helper)."""
    return _infer_sources(repo_root, source_root, ProjectMetadata())[0]


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
    return rel_path(value, key)


def _argv_path(value: object, key: str = "testpaths") -> str:
    """A repo-relative path that is spliced into pytest's argv as a word of its own.

    It must not start with ``-`` (pytest would take it for an option, e.g. ``-p evil``) or ``@``
    (argparse would read further arguments from that file).
    """
    path = _rel_path(value, key)
    if path.startswith(("-", "@")):
        raise ConfigError(f"{key} entries must not start with '-' or '@': {_shown(value)}")
    return path


def _pytest_args(value: object) -> tuple[str, ...]:
    """Validate ``[tool.coverset] pytest_args``: options only, none of the known conflicts.

    Every word must be an option (a positional path would run in every shard on top of its args
    file) or the value of one of the short options that take one (``-m``, ``-W``, ``-o``, ``-p``,
    ``-r``); long options carry their value as ``--opt=value``. Refused: options that write outside
    the worker or move pytest's root (``--basetemp``, ``--rootdir``, ``-c``, ``--junitxml``,
    ``--log-file``, ``--report-log``...), that choose tests by name or path or stop a shard early
    (``-k``, ``--deselect``, ``--ignore*``, ``--pyargs``, ``-x``, ``--maxfail``, ``--lf``...), that
    coverset sets itself (``-n``, ``--cov*``, ``--coverset-*``), ``-p`` other than ``-p no:NAME``
    (loading plugins is the repository's ``conftest.py``'s job, not an argv's), and ``-o`` keys in
    :data:`_OVERRIDE_INI_DENIED`; an ``-o addopts=...`` value is checked by these same rules.
    ``-m`` stays allowed: a marker expression filters the whole suite the same way at collection and
    in every shard. This blocks mistakes and known conflicts; it is not a sandbox (the repository's
    ``conftest.py`` runs in the worker regardless).
    """
    return _pytest_words(_str_list(value, "pytest_args"), nested=False)


def _override_ini(value: str, *, nested: bool) -> None:
    """Refuse an ``-o KEY=VALUE`` that :func:`_pytest_args` does not allow (see there)."""
    key, sep, rest = value.partition("=")
    key = key.strip()
    if not sep or not key:
        raise ConfigError(f"pytest_args: -o needs KEY=VALUE, got {_shown(value)}")
    if key in _OVERRIDE_INI_DENIED or key.startswith("verbosity"):
        raise ConfigError(f"pytest_args: -o {_shown(key)} is managed by coverset or unsafe")
    if key == "addopts":
        if nested:
            raise ConfigError("pytest_args: an addopts override may not override addopts again")
        try:
            words = shlex.split(rest)
        except ValueError as exc:
            raise ConfigError(f"pytest_args: -o addopts does not parse: {exc}") from exc
        _pytest_words(words, nested=True)


def _pytest_words(words: list[str], *, nested: bool) -> tuple[str, ...]:
    out: list[str] = []
    expecting: str | None = None
    for word in words:
        if not word or "\0" in word or len(word) > 1024 or "\n" in word:
            raise ConfigError("pytest_args entries must be non-empty single-line strings")
        if expecting is not None:
            if word.startswith(("-", "@")):
                raise ConfigError(f"pytest_args: {expecting} needs a value, got {_shown(word)}")
            if expecting == "-p" and not word.startswith("no:"):
                raise ConfigError("pytest_args: -p may only disable a plugin (-p no:NAME)")
            if expecting == "-o":
                _override_ini(word, nested=nested)
            out.append(word)
            expecting = None
            continue
        if not word.startswith("-") or word == "-":
            raise ConfigError(
                f"pytest_args: {_shown(word)} is not an option (positional paths would run in every shard)"
            )
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if any(
                name == denied
                or name.startswith(denied + "-")
                or (denied in ("--cov", "--coverset") and name.startswith(denied))
                for denied in _PYTEST_DENIED_LONG
            ):
                raise ConfigError(f"pytest_args: {_shown(name)} is managed by coverset or unsafe")
            if name == "--override-ini":
                if "=" not in word:
                    raise ConfigError("pytest_args: write --override-ini=KEY=VALUE")
                _override_ini(word.split("=", 1)[1], nested=nested)
        else:
            short = word[:2]
            if short in _PYTEST_VALUE_SHORT:
                attached = word[2:].lstrip("=")
                if not attached:
                    expecting = short
                elif short == "-p" and not attached.startswith("no:"):
                    raise ConfigError("pytest_args: -p may only disable a plugin (-p no:NAME)")
                elif short == "-o":
                    _override_ini(attached, nested=nested)
            elif _PYTEST_DENIED_SHORT.intersection(word[1:]):
                raise ConfigError(f"pytest_args: {_shown(word)} is managed by coverset or unsafe")
        out.append(word)
    if expecting is not None:
        raise ConfigError(f"pytest_args: {expecting} needs a value")
    return tuple(out)


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
    return shown(value)
