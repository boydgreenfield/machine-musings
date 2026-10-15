"""``POST /v1/bundles`` above the app's global 25 MiB body limit.

``coverset-ci upload`` sends chunked bodies; behind a proxy that answers 411 it sends the archive
again with a ``Content-Length``. That declared length used to hit ``MAX_CONTENT_LENGTH`` before the
view ran. The upload route is now exempt and its request carries ``COVERSET_MAX_UPLOAD_BYTES``
instead, so a 30 MiB archive is accepted under a large cap and refused by the view's own 413 (after
authorization) under a small one. The real bundle and helpers are ``test_uploads``'s.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import test_uploads as uploads
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select

from coverset.app import MAX_CONTENT_LENGTH
from coverset.db.models import Repo, Run

# test_uploads's fixtures, shared by name (the real bundle, the fake forge, the repository row)
bundle_dir = uploads.bundle_dir
forge = uploads.forge
repo = uploads.repo
headers, make_archive, make_token, settle = (
    uploads.headers,
    uploads.make_archive,
    uploads.make_token,
    uploads.settle,
)

MIB = 1024 * 1024
BODY = 30 * MIB


@pytest.fixture(params=["large", "small"])
def cap(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def settings_overrides(cap: str) -> dict[str, Any]:
    return {
        "admin_token": uploads.ADMIN,
        "allowed_owners": ["acme"],
        "forge": "fake",
        "ai": "off",
        "max_upload_bytes": 64 * MIB if cap == "large" else 16 * MIB,
    }


def padded_archive(bundle: Path) -> bytes:
    """A real upload archive grown past 30 MiB by an unknown member (the server drops those)."""
    data = make_archive(bundle, raw_members=[("padding.bin", os.urandom(BODY))])
    assert BODY < len(data) < 32 * MIB and len(data) > MAX_CONTENT_LENGTH
    return data


async def test_a_declared_30_mib_upload(
    app: Quart,
    client: Client,
    repo: Repo,
    forge: Any,
    bundle_dir: Path,
    cap: str,
) -> None:
    token = await make_token(app, repo.id, ["upload"])
    body = padded_archive(bundle_dir)
    declared = {"Content-Length": str(len(body))}
    # authorization still comes first: no credentials is a 401, not a 413
    anonymous = await client.post("/v1/bundles", headers={**headers(None), **declared}, data=body)
    assert anonymous.status_code == 401
    response = await client.post("/v1/bundles", headers={**headers(token), **declared}, data=body)
    payload = await response.get_json()
    if cap == "small":
        # the view's own 413 (JSON, with its slug), not the global limit's
        assert response.status_code == 413 and payload["error"] == "too_large", payload
        return
    assert response.status_code == 202, payload
    await settle(app)
    async with app.extensions["db"].session() as session:
        run = await session.scalar(select(Run).where(Run.id == payload["run_id"]))
    assert run is not None and run.repo_id == repo.id


async def test_other_routes_keep_the_global_limit(client: Client, cap: str) -> None:
    size = MAX_CONTENT_LENGTH + 1
    for path in ("/webhooks/github", "/v1/bundles/x"):
        response = await client.post(path, data=b"x" * size, headers={"Content-Length": str(size)})
        assert response.status_code == 413, (path, cap)
