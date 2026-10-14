"""The cache knobs as settings: ``COVERSET_FACTS_*``, ``COVERSET_RUN_FACTS_*``,
``COVERSET_SOURCE_CACHE_MB`` and ``COVERSET_AGENT_CONTEXTS`` are :class:`Settings` fields, the app
hands them to the modules, and the modules keep the environment as their fallback."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from coverset.agent import backends
from coverset.app import create_app
from coverset.config import CACHE_SETTINGS, Settings
from coverset.facts import cache
from coverset.web import loaders

ROOT = Path(__file__).resolve().parents[1]


def test_every_cache_knob_is_a_setting_with_its_environment_name() -> None:
    assert set(CACHE_SETTINGS.values()) == {
        "COVERSET_FACTS_CACHE_ENTRIES",
        "COVERSET_FACTS_MEMORY_LIMIT",
        "COVERSET_FACTS_THREADS",
        "COVERSET_FACTS_RESULT_ENTRIES",
        "COVERSET_RUN_FACTS_ENTRIES",
        "COVERSET_RUN_FACTS_MB",
        "COVERSET_SOURCE_CACHE_MB",
        "COVERSET_FACTS_COMPACT",
        "COVERSET_AGENT_CONTEXTS",
    }
    for field, env_name in CACHE_SETTINGS.items():
        assert env_name == "COVERSET_" + field.upper()
        assert Settings.model_fields[field].default is None
    example = (ROOT / ".env.example").read_text()
    assert all(f"\n{name}=" in example for name in CACHE_SETTINGS.values()), ".env.example documents them"


def test_unset_knobs_give_no_overrides() -> None:
    assert Settings.for_tests().cache_overrides() == {}


def test_overrides_carry_the_set_values() -> None:
    settings = Settings.for_tests(
        facts_threads=2, facts_compact=True, facts_memory_limit="1.5GiB", run_facts_mb=128
    )
    assert settings.cache_overrides() == {
        "COVERSET_FACTS_THREADS": "2",
        "COVERSET_FACTS_COMPACT": "1",
        "COVERSET_FACTS_MEMORY_LIMIT": "1.5GiB",
        "COVERSET_RUN_FACTS_MB": "128",
    }
    assert Settings.for_tests(facts_compact=False).cache_overrides() == {"COVERSET_FACTS_COMPACT": "0"}


@pytest.mark.parametrize(
    ("field", "value"),
    [("facts_memory_limit", "1GB'; SET threads=64; --"), ("facts_threads", 0), ("run_facts_mb", -1)],
)
def test_bad_values_are_refused_at_startup(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.for_tests(**{field: value})


def test_the_environment_and_dotenv_feed_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("COVERSET_SOURCE_CACHE_MB=32\nCOVERSET_AGENT_CONTEXTS=5\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COVERSET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COVERSET_AGENT_CONTEXTS", "6")  # the environment outranks .env
    for name in ("COVERSET_SOURCE_CACHE_MB", "COVERSET_RUN_FACTS_MB"):
        monkeypatch.delenv(name, raising=False)
    overrides = Settings().cache_overrides()
    assert overrides["COVERSET_SOURCE_CACHE_MB"] == "32" and overrides["COVERSET_AGENT_CONTEXTS"] == "6"


def test_create_app_configures_the_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in CACHE_SETTINGS.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COVERSET_FACTS_THREADS", "3")
    before = (loaders.run_facts_entries(), loaders.run_facts_budget(), loaders.blob_budget())
    assert before == (loaders.RUN_FACTS_ENTRIES, loaders.RUN_FACTS_BUDGET, loaders.BLOB_BUDGET)
    assert backends.context_cache_size() == backends.CONTEXT_CACHE and cache.threads() == 3
    settings = Settings.for_tests(
        data_dir=tmp_path / "data",
        facts_threads=2,
        run_facts_entries=4,
        run_facts_mb=128,
        source_cache_mb=16,
        agent_contexts=7,
    )
    create_app(settings)
    assert cache.threads() == 2, "the setting wins over the environment"
    assert loaders.run_facts_entries() == 4 and loaders.run_facts_budget() == 128 * 1024 * 1024
    assert loaders.blob_budget() == 16 * 1024 * 1024
    assert backends.context_cache_size() == 7
    create_app(Settings.for_tests(data_dir=tmp_path / "data2"))
    assert cache.threads() == 3, "an app without the knob leaves the environment in charge"
    assert loaders.run_facts_entries() == loaders.RUN_FACTS_ENTRIES


def test_the_cli_context_configures_the_modules(tmp_path: Path) -> None:
    from coverset.orchestrator.runner import OrchestratorContext

    OrchestratorContext.from_settings(Settings.for_tests(data_dir=tmp_path / "data", agent_contexts=9))
    assert backends.context_cache_size() == 9


def test_the_source_budget_bounds_the_blob_cache(tmp_path: Path) -> None:
    loaders.clear_caches()
    try:
        cache.configure({"COVERSET_SOURCE_CACHE_MB": "1"})
        big = b"x" * (700 * 1024)
        loaders._remember_blobs(tmp_path, "a" * 40, {"one.py": big, "two.py": big})
        assert loaders._cached_blob(tmp_path, "a" * 40, "one.py") is None, "evicted past 1 MB"
        assert loaders._cached_blob(tmp_path, "a" * 40, "two.py") is not None
    finally:
        loaders.clear_caches()


# -- the test suite's own conftest ------------------------------------------------------------------


def _conftest() -> object:
    spec = importlib.util.spec_from_file_location(
        "coverset_tests_root_conftest", ROOT / "tests" / "conftest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_command_line_paths_are_grouped_by_directory() -> None:
    grouped = _conftest().grouped_by_directory  # type: ignore[attr-defined]
    args = [
        "tests/web/test_static_caching.py",
        "tests/test_config.py::test_x",
        "tests/web/test_web_auth.py",
        "tests/orchestrator",
        "tests/web/test_account.py",
        "no-such-path",
    ]
    out = grouped(args, ROOT)
    assert sorted(out) == sorted(args)
    web = [a for a in out if a.startswith("tests/web/")]
    first = out.index(web[0])
    assert out[first : first + len(web)] == web, "the tests/web paths are contiguous"
    assert web == [
        "tests/web/test_static_caching.py",
        "tests/web/test_web_auth.py",
        "tests/web/test_account.py",
    ]


def test_interleaved_directories_collect_one_node_per_directory() -> None:
    """pytest 9 binds conftest fixtures to one ``Dir`` node; interleaved paths used to create a second
    ``Dir web`` whose tests could not see ``tests/web/conftest.py`` ("fixture 'demo' not found")."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "tests/web/test_static_caching.py",
            "tests/test_config.py",
            "tests/web/test_web_auth.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert proc.stdout.count("<Dir web>") == 1, proc.stdout
