"""Fixtures for the facts-store tests (helpers live in ``facts_helpers.py``)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from facts_helpers import build_minimal_bundle, calc_functions

from coverset.facts.store import FactsStore
from coverset.lang.base import FunctionRegion


@pytest.fixture(scope="session")
def minimal_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A freshly generated minimal bundle directory (shared, treat as read-only)."""
    return build_minimal_bundle(tmp_path_factory.mktemp("minimal"))


@pytest.fixture
def bundle_copy(minimal_bundle: Path, tmp_path: Path) -> Path:
    """A private, mutable copy of the minimal bundle directory."""
    return Path(shutil.copytree(minimal_bundle, tmp_path / "bundle"))


@pytest.fixture
def store(tmp_path: Path) -> FactsStore:
    """An empty facts store."""
    return FactsStore(tmp_path / "facts")


@pytest.fixture
def functions() -> list[FunctionRegion]:
    """Function regions of the fixture module."""
    return calc_functions()
