"""``coverset demo --no-serve`` end to end through the CLI: the runs, the replay and the static report."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from coverset.cli import cli

pytestmark = pytest.mark.slow


@pytest.mark.skipif(
    shutil.which("uv") is None or shutil.which("git") is None, reason="the demo needs uv and git"
)
def test_demo_cli_builds_the_static_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("COVERSET_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COVERSET_ENV", "test")
    monkeypatch.delenv("COVERSET_EXECUTOR", raising=False)
    result = CliRunner().invoke(cli, ["demo", "--executor", "local", "--no-serve"], catch_exceptions=False)
    assert result.exit_code == 0, result.output[-2000:]
    assert "static report of PR #2" in result.output
    index = data_dir / "report" / "index.html"
    assert index.is_file() and (data_dir / "report" / ".coverset-report").exists()
    assert "coverset demo · PR #2" in index.read_text()
    assert (data_dir / "report" / "functions").is_dir()
