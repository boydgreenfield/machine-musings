"""``pytest --coverset``: the local switch, its defaults and the output directory's safety rules.

``--coverset`` (or ``COVERSET=1`` in the environment) turns instrumentation on with defaults a
developer wants on a laptop: the bundle goes to ``<rootdir>/.coverset``, the measured packages come
from ``[tool.coverset]`` in ``<rootdir>/pyproject.toml`` (``source``, else the packages found under
``source_root`` as the control plane infers them), the neutral export is on, and one summary line is
printed at the end. Any explicit ``--coverset-*`` option still wins. The runner never uses the switch.

The output directory, local or not, must not be a symlink; when the plugin creates it, it puts a
``.gitignore`` holding ``*`` inside, so a bundle is never committed by accident.
"""

from __future__ import annotations

import os
import re
import shlex
import tomllib
from collections.abc import Mapping, MutableMapping
from pathlib import Path, PurePosixPath

import pytest

SWITCH_ENV = "COVERSET"
DEFAULT_OUT = ".coverset"
GITIGNORE = ".gitignore"
GITIGNORE_TEXT = "# Created by pytest-coverset: nothing in this directory belongs in git.\n*\n"
MAX_PYPROJECT_BYTES = 1024 * 1024
_TRUE = frozenset({"1", "true", "yes", "on"})
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_NOT_SOURCE_PACKAGES = frozenset({"tests", "test", "docs", "examples", "scripts", "benchmarks"})
"""Directories never inferred as sources (the control plane's ``lang/python.py`` list)."""


def pop_switch(environ: MutableMapping[str, str] | None = None) -> bool:
    """Read ``COVERSET`` (``1``, ``true``, ``yes``, ``on``) and remove it from the environment.

    Removing it keeps pytest runs that the tests start (subprocesses, ``pytester``) uninstrumented.
    """
    env = os.environ if environ is None else environ
    return env.pop(SWITCH_ENV, "").strip().lower() in _TRUE


def sources_from_pyproject(rootdir: Path) -> tuple[str, ...]:
    """The packages ``[tool.coverset]`` in ``rootdir/pyproject.toml`` names, else the inferred ones.

    ``source`` (a module name or a list of them) wins; without it, the top-level packages (a
    directory with ``__init__.py``) under ``source_root`` (default ``src`` when it is a directory,
    else ``.``) are used, minus ``tests``, ``docs`` and the like, as the control plane infers them.
    Raises :class:`pytest.UsageError` when the table is malformed or nothing is found.
    """
    pyproject = rootdir / "pyproject.toml"
    table: Mapping[str, object] = {}
    if pyproject.is_file():
        try:
            with pyproject.open("rb") as fh:
                data = fh.read(MAX_PYPROJECT_BYTES + 1)
            if len(data) > MAX_PYPROJECT_BYTES:
                raise ValueError("larger than 1 MiB")
            doc = tomllib.loads(data.decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise pytest.UsageError(f"--coverset: cannot read {pyproject}: {exc}") from exc
        tool = doc.get("tool")
        raw = tool.get("coverset", {}) if isinstance(tool, dict) else {}
        if not isinstance(raw, dict):
            raise pytest.UsageError(f"--coverset: [tool.coverset] in {pyproject} must be a table")
        table = raw
    if "source" in table:
        return _module_names(table["source"], pyproject)
    source_root = _source_root(table.get("source_root"), rootdir, pyproject)
    found = _infer_packages(rootdir, source_root)
    if not found:
        raise pytest.UsageError(
            f"--coverset found no package to measure under {rootdir / source_root}: set "
            f'[tool.coverset] source = ["yourpkg"] in {pyproject}, or pass --coverset-source=PKG'
        )
    return found


def _module_names(value: object, pyproject: Path) -> tuple[str, ...]:
    names = [value] if isinstance(value, str) else value
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(n, str) and _MODULE.fullmatch(n) for n in names)
    ):
        raise pytest.UsageError(
            f"--coverset: [tool.coverset] source in {pyproject} must be a module name or a "
            'non-empty list of them, such as ["yourpkg"]'
        )
    return tuple(dict.fromkeys(names))


def _source_root(value: object, rootdir: Path, pyproject: Path) -> str:
    if value is None:
        return "src" if _inside_dir(rootdir, "src") else "."
    if not isinstance(value, str) or not value:
        raise pytest.UsageError(f"--coverset: [tool.coverset] source_root in {pyproject} must be a path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise pytest.UsageError(
            f"--coverset: [tool.coverset] source_root in {pyproject} must be a relative path inside the "
            "project"
        )
    return path.as_posix()


def _inside_dir(rootdir: Path, rel: str) -> bool:
    path = rootdir / rel
    try:
        return path.is_dir() and path.resolve().is_relative_to(rootdir.resolve())
    except OSError:
        return False


def _infer_packages(rootdir: Path, source_root: str) -> tuple[str, ...]:
    if not _inside_dir(rootdir, source_root):
        return ()
    found = [
        child.name
        for child in (rootdir / source_root).iterdir()
        if not child.is_symlink()
        and child.is_dir()
        and (child / "__init__.py").is_file()
        and _MODULE.fullmatch(child.name)
        and child.name not in _NOT_SOURCE_PACKAGES
    ]
    return tuple(sorted(found))


def prepare_out_dir(out_dir: Path) -> bool:
    """Create ``out_dir`` if needed; returns True when this call created it.

    A symlink (even one to a directory) or a non-directory is refused with a usage error, so the
    bundle is never written through a link. A directory created here gets a ``.gitignore`` of ``*``.
    """
    if out_dir.is_symlink():
        raise pytest.UsageError(f"coverset output directory {out_dir} is a symlink: pass a real directory")
    if out_dir.exists() and not out_dir.is_dir():
        raise pytest.UsageError(f"coverset output directory {out_dir} exists and is not a directory")
    created = False
    try:
        out_dir.mkdir(parents=True)
        created = True
    except FileExistsError:
        if out_dir.is_symlink() or not out_dir.is_dir():
            raise pytest.UsageError(f"coverset output directory {out_dir} is not a real directory") from None
    if created:
        (out_dir / GITIGNORE).write_text(GITIGNORE_TEXT, encoding="utf-8")
    return created


def display_path(path: Path, cwd: str, root: str) -> str:
    """``path`` relative to ``cwd`` (``../.coverset`` from a subdirectory) when both lie inside
    ``root``, else absolute."""
    full, here, top = (os.path.realpath(p) for p in (path, cwd, root))
    if os.path.commonpath([full, top]) != top or os.path.commonpath([here, top]) != top:
        return full
    return Path(os.path.relpath(full, here)).as_posix()


def summary_line(out: str, tests: int, files: int, errors: int) -> str:
    """The one line ``--coverset`` prints after the bundle is written."""
    quoted = shlex.quote(out)
    line = f"coverset: wrote {out} ({_plural(tests, 'test')}, {_plural(files, 'file')}); "
    line += f"`coverset report {quoted}` for the pages"
    if errors:
        line += f" ({_plural(errors, 'error')} in {out}/meta.json)"
    return line


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
