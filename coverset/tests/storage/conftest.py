"""Fixtures for the blob-store tests: every test runs against the local directory store and an S3
store (moto in-process, or the real store ``COVERSET_TEST_BLOB_URL`` names; see
:mod:`coverset.storage.testing`), each test under its own key prefix."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from coverset.storage import BlobStore, LocalBlobStore, S3BlobStore
from coverset.storage.testing import fresh_blob_url


@pytest.fixture
def s3_store() -> Iterator[S3BlobStore]:
    """An S3 store under a fresh key prefix (deleted afterwards)."""
    store = S3BlobStore.from_url(fresh_blob_url("blob"))
    yield store
    store.delete([info.key for info in store.list()])  # everything under this test's prefix


@pytest.fixture(params=["local", "s3"])
def blobs(request: pytest.FixtureRequest, tmp_path: Path) -> BlobStore:
    """The same tests against both backends."""
    if request.param == "local":
        return LocalBlobStore(tmp_path / "blobs")
    return request.getfixturevalue("s3_store")
