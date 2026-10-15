"""The orchestrator's CLI commands (the cheap ones; ``run`` and ``demo`` are covered end to end)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from orch_fakes import PROJECT_ROOT

from coverset.cli import cli
from coverset.commands.common import StreamPrinter
from coverset.commands.facts import format_rows, match_function
from coverset.config import Settings
from coverset.lang.base import FunctionRegion


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> CliRunner:
    """The CLI on the ``settings`` fixture's data dir and database."""
    monkeypatch.setenv("COVERSET_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("COVERSET_DATABASE_URL", settings.database_url)
    from coverset.config import get_settings

    get_settings.cache_clear()
    yield CliRunner()  # type: ignore[misc]
    get_settings.cache_clear()


def test_commands_replace_their_placeholders() -> None:
    for name in ("run", "warm", "demo", "shapes", "gc"):
        assert "arrives in phase" not in (cli.commands[name].help or ""), name
    groups = {name: cli.commands[name] for name in ("facts", "invariants", "repo", "modal")}
    assert {"query"} <= set(groups["facts"].commands)  # type: ignore[attr-defined]
    assert "check" in groups["invariants"].commands  # type: ignore[attr-defined]
    assert "purge" in groups["repo"].commands  # type: ignore[attr-defined]
    assert "reap" in groups["modal"].commands  # type: ignore[attr-defined]


def test_invariants_check(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli, ["invariants", "check", "--repo", str(PROJECT_ROOT / "examples" / "demo-app")]
    )
    assert result.exit_code == 0, result.output
    assert "cents_conserved" in result.output and "4 invariant(s) valid" in result.output
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "invariants.yaml").write_text(
        "invariants:\n  - id: x\n    check: !!python/object:os.system x\n"
    )
    result = runner.invoke(cli, ["invariants", "check", "--repo", str(broken)])
    assert result.exit_code == 1 and "invariants.yaml" in result.output
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "no invariants file" in runner.invoke(cli, ["invariants", "check", "--repo", str(empty)]).output


@pytest.mark.security
def test_facts_query_allows_only_select(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["facts", "query", "SELECT 1 AS one"])
    assert result.exit_code == 0 and "one" in result.output and "(1 row)" in result.output
    result = runner.invoke(cli, ["facts", "query", "COPY (SELECT 1) TO 'x.csv'"])
    assert result.exit_code == 1 and "SELECT" in result.output


def test_gc_dry_run_on_an_empty_store(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["gc", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would delete 0 run(s)" in result.output


def test_stream_printer_formats_events() -> None:
    printer = StreamPrinter()
    assert printer.format(
        "test", {"nodeid": "t.py::a", "outcome": "passed", "duration_s": 0.5, "shard": 1}
    ) == ("PASSED   t.py::a [0.50s, shard 1]")
    assert "flaky" in (printer.format("test", {"nodeid": "x", "outcome": "passed", "flaky": True}) or "")
    assert (
        printer.format("done", {"counts": {"passed": 2, "failed": 0}, "exit_code": 0})
        == "== 2 passed == exit 0"
    )
    assert printer.format("log", {"shard": 0, "text": "x"}) is None
    assert StreamPrinter(verbose=True).format("log", {"shard": 0, "text": "x"}) == "  [shard 0] x"


def test_format_rows_and_function_matching() -> None:
    text = format_rows(["a", "b"], [{"a": 1, "b": "x" * 100}])
    assert text.splitlines()[0].startswith("a") and "..." in text and text.endswith("(1 row)")

    def region(path: str, qualname: str, key: str) -> FunctionRegion:
        return FunctionRegion(key, path, qualname, 0, 1, 2, 3, "h", "s", (), "function")

    regions = [
        region("src/pkg/a.py", "f", "k1"),
        region("src/pkg/b.py", "f", "k2"),
        region("src/pkg/b.py", "g", "k3"),
    ]
    assert match_function(regions, "g").func_key == "k3"
    assert match_function(regions, "pkg.a.f").func_key == "k1"
    assert match_function(regions, "k2").func_key == "k2"
    with pytest.raises(Exception, match="ambiguous"):
        match_function(regions, "f")
    with pytest.raises(Exception, match="no function"):
        match_function(regions, "nope")


_SIGTERM_SCRIPT = """
import asyncio, sys
from pathlib import Path
from coverset.commands.common import run_async

out = Path(sys.argv[1])


async def fake_run():
    try:
        out.joinpath("started").write_text("1")
        await asyncio.sleep(60)  # a run holding workers
    finally:
        out.joinpath("released").write_text("1")  # what execute_run's and ctx.close()'s finally do


run_async(fake_run)
"""


@pytest.mark.security
def test_sigterm_cancels_the_command_so_finally_blocks_release_workers(tmp_path: Path) -> None:
    import os
    import signal
    import subprocess
    import sys
    import time

    from coverset.commands.common import SIGTERM_EXIT_CODE

    script = tmp_path / "sigterm_run.py"
    script.write_text(_SIGTERM_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script), str(tmp_path)], env={**os.environ})
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "started").exists():
            assert proc.poll() is None and time.monotonic() < deadline, "the fake run never started"
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == SIGTERM_EXIT_CODE == 143
    finally:
        if proc.poll() is None:
            proc.kill()
    assert (tmp_path / "released").read_text() == "1", "the finally block ran"
