"""Fixtures for the facts-store tests (helpers live in ``facts_helpers.py``).

``store`` is parametrized: every test that takes it runs against the local default (``local``) and
against an S3 object store with the directory as its cache (``s3``: moto in-process, or the store
``COVERSET_TEST_BLOB_URL`` names, each test under its own key prefix; :mod:`coverset.storage.testing`).
The two must answer every question the same way.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
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


@pytest.fixture(params=["local", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[FactsStore]:
    """An empty facts store: local files, or the cache of an S3 store (see the module docstring)."""
    if request.param == "local":
        yield FactsStore(tmp_path / "facts")
        return
    from coverset.storage import S3BlobStore
    from coverset.storage.testing import fresh_blob_url

    blobs = S3BlobStore.from_url(fresh_blob_url("facts"))
    yield FactsStore(tmp_path / "facts", blobs=blobs)
    blobs.delete([info.key for info in blobs.list()])  # this test's prefix only


@pytest.fixture
def functions() -> list[FunctionRegion]:
    """Function regions of the fixture module."""
    return calc_functions()
