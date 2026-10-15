"""Fixtures for the analysis tests: throwaway git repositories."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from analysis_helpers import git


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """An empty git repository on branch ``main`` with a hermetic identity."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    return repo
