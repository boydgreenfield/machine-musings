"""Live Modal check (spends well under a cent): only with ``COVERSET_MODAL_LIVE=1`` and a Modal token.

Run with ``COVERSET_MODAL_LIVE=1 uv run pytest -m modal tests/executors/test_modal_live.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

pytestmark = [
    pytest.mark.modal,
    pytest.mark.skipif(
        os.environ.get("COVERSET_MODAL_LIVE") != "1", reason="set COVERSET_MODAL_LIVE=1 to run"
    ),
]


def test_modal_smoke_command(tmp_path: Path) -> None:
    from coverset.cli import cli

    result = CliRunner().invoke(
        cli, ["modal", "smoke"], env={"COVERSET_DATA_DIR": str(tmp_path / "data")}, catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert "Python 3.13" in result.output and "modal smoke: ok" in result.output
