"""Environment export: the plugin wheel cache, env hashes and executor selection."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from coverset.config import Settings
from coverset.executors import ExecutorError, env_export, get_executor
from coverset.executors.env_export import (
    PLUGIN_DIR,
    build_env_spec,
    build_plugin_wheel,
    env_hash,
    plugin_source_hash,
)
from coverset.executors.local import LocalExecutor
from coverset.lang.base import EnvSpec


def test_plugin_source_hash_tracks_sources(tmp_path: Path) -> None:
    copy = tmp_path / "plugin"
    shutil.copytree(PLUGIN_DIR, copy, ignore=shutil.ignore_patterns("tests", "__pycache__", ".venv"))
    first = plugin_source_hash(copy)
    assert first == plugin_source_hash(copy)
    (copy / "src" / "pytest_coverset" / "__pycache__").mkdir(exist_ok=True)
    (copy / "src" / "pytest_coverset" / "__pycache__" / "x.pyc").write_bytes(b"junk")
    assert plugin_source_hash(copy) == first
    (copy / "src" / "pytest_coverset" / "extra.py").write_text("X = 1\n")
    assert plugin_source_hash(copy) != first


def test_plugin_wheel_is_built_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("needs uv")
    monkeypatch.setattr(env_export, "_wheel_memo", {})
    wheel = build_plugin_wheel(cache_dir=tmp_path)
    assert wheel.suffix == ".whl" and wheel.is_file() and wheel.is_relative_to(tmp_path)
    monkeypatch.setattr(env_export, "_wheel_memo", {})
    monkeypatch.setattr(env_export, "_build_into", lambda *a: pytest.fail("rebuilt a cached wheel"))
    assert build_plugin_wheel(cache_dir=tmp_path) == wheel


def test_missing_plugin_sources_are_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ExecutorError, match="sources not found"):
        build_plugin_wheel(tmp_path / "nowhere", cache_dir=tmp_path)


def test_env_hash_matches_the_adapter_formula(tmp_path: Path) -> None:
    class Adapter:
        def env_spec(self, repo_root: Path, cfg: dict) -> EnvSpec:  # type: ignore[type-arg]
            return EnvSpec("3.13", "l", "req\n", cfg.get("plugin_wheel"), ".", env_hash("req\n", "3.13", "w"))

    spec = build_env_spec(Adapter(), tmp_path, {"python": "3.13"}, None)  # type: ignore[arg-type]
    assert spec.env_hash == env_hash("req\n", "3.13", "w") != env_hash("req\n", "3.12", "w")
    assert env_hash("a", "3.13", None) == env_hash("a", "3.13", "none")


def test_plugin_pins_join_the_requirements_and_the_hash(tmp_path: Path) -> None:
    pins = env_export.plugin_runtime_pins()
    assert set(pins) == {"coverage", "pytest"}  # read from coverset's own uv.lock

    class Adapter:
        def __init__(self, requirements: str) -> None:
            self.requirements = requirements

        def env_spec(self, repo_root: Path, cfg: dict) -> EnvSpec:  # type: ignore[type-arg]
            return EnvSpec("3.13", "l", self.requirements, Path(cfg["plugin_wheel"]), ".", "h" * 64)

    wheel = tmp_path / "x.whl"
    bare = build_env_spec(Adapter("attrs==25.1.0\n"), tmp_path, {}, wheel)  # type: ignore[arg-type]
    added = [f"coverage=={pins['coverage']}", f"pytest=={pins['pytest']}"]
    assert bare.requirements_txt == "attrs==25.1.0\n" + "".join(f"{p}\n" for p in added)
    assert bare.env_hash == hashlib.sha256("\0".join(("h" * 64, *added)).encode()).hexdigest()
    assert bare.plugin_wheel == wheel
    # a repo that pins its own coverage and pytest keeps them, and its env hash
    own = "coverage[toml]==7.6.1\npytest==8.3.0\n"
    kept = build_env_spec(Adapter(own), tmp_path, {}, wheel)  # type: ignore[arg-type]
    assert (kept.requirements_txt, kept.env_hash) == (own, "h" * 64)
    # pytest mentioned without a pin is left alone; coverage still gets its pin
    loose = build_env_spec(Adapter("pytest>=8\n"), tmp_path, {}, wheel)  # type: ignore[arg-type]
    assert loose.requirements_txt == f"pytest>=8\ncoverage=={pins['coverage']}\n"
    assert env_export.with_plugin_pins(kept, {}) is kept


def test_get_executor(tmp_path: Path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path)
    assert isinstance(get_executor(settings, "local"), LocalExecutor)
    try:
        import coverset.executors.modal_  # noqa: F401
    except ImportError:
        with pytest.raises(ExecutorError, match="Modal executor is not available"):
            get_executor(settings, "modal")
