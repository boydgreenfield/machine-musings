"""Fixtures for the pytest-coverset tests."""

from __future__ import annotations

import shutil

import pytest
from support import FIXTURE_PROJECT, NONCE

pytest_plugins = ["pytester"]
collect_ignore = ["fixture_project"]


@pytest.fixture
def project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A pytester dir holding a copy of ``tests/fixture_project`` with the event nonce set."""
    shutil.copytree(
        FIXTURE_PROJECT, pytester.path, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )
    monkeypatch.setenv("COVERSET_EVENT_NONCE", NONCE)
    monkeypatch.delenv("COVERAGE_PROCESS_START", raising=False)
    monkeypatch.delenv("COVERSET", raising=False)
    return pytester
