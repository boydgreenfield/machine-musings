"""Upload mode with an object store (``COVERSET_BLOB_URL`` on moto, or ``COVERSET_TEST_BLOB_URL``).

The same ``POST /v1/bundles`` as ``test_uploads.py`` (its archive builder, token and fixtures are
reused), but nothing a restart would lose stays on local disk only: the shard archive, the uploaded
sources and the facts are objects. A "fresh replica" (an empty cache directory and an empty runs
directory) then serves the source view, reprocesses the run from the archives in the bucket, and a
purge leaves no object behind.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select
from test_uploads import (  # noqa: F401 - fixtures are used by name
    ADMIN,
    DEMO_APP,
    SHA,
    _no_network_git,
    bundle_dir,
    forge,
    headers,
    make_archive,
    make_token,
    repo,
    settle,
)

from coverset.db.models import Repo, Run
from coverset.facts.gc import purge_repo_files
from coverset.forge.fake import FakeForge
from coverset.orchestrator.upload import process_run
from coverset.storage import store_from_url
from coverset.storage.testing import fresh_blob_url


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    """``test_uploads.py``'s settings plus a fresh prefix of the test bucket."""
    return {
        "admin_token": ADMIN,
        "allowed_owners": ["acme"],
        "forge": "fake",
        "ai": "off",
        "blob_url": fresh_blob_url("uploads"),
    }


async def test_an_upload_lives_in_the_bucket_and_survives_a_fresh_replica(
    app: Quart,
    client: Client,
    repo: Repo,  # noqa: F811 - the fixture imported from test_uploads
    forge: FakeForge,  # noqa: F811
    bundle_dir: Path,  # noqa: F811
) -> None:
    settings = app.extensions["settings"]
    blobs = store_from_url(settings.blob_url)
    token = await make_token(app, repo.id, ["upload"])
    parse = (DEMO_APP / "src" / "ledger" / "durations" / "parse.py").read_bytes()
    archive = make_archive(bundle_dir, sources={"src/ledger/durations/parse.py": parse})
    response = await client.post("/v1/bundles", headers=headers(token), data=archive)
    assert response.status_code == 202, await response.get_data(as_text=True)
    run_id = (await response.get_json())["run_id"]
    await settle(app)

    keys = {info.key for info in blobs.list()}
    assert f"uploads/{repo.id}/{SHA}/py313.tar.gz" in keys
    assert f"sources/{repo.id}/{SHA}/src/ledger/durations/parse.py" in keys
    assert f"facts/repo={repo.id}/run={run_id}/MANIFEST.json" in keys
    assert f"facts/repo={repo.id}/dictionary.json" in keys
    assert not (settings.data_dir / "sources").exists(), "sources are objects, cached under the cache dir"
    async with app.extensions["db"].session() as session:
        first = await session.get(Run, run_id)
        assert first is not None and first.status == "completed"
        verdict = first.verdict

    url = f"/repos/{repo.id}/files/src/ledger/durations/parse.py?run={run_id}"
    auth = {"Authorization": f"Bearer {ADMIN}"}
    assert "parse_duration" in await (await client.get(url, headers=auth)).get_data(as_text=True)

    # A fresh replica: nothing in the cache, nothing under the runs directory.
    shutil.rmtree(settings.cache_dir)
    shutil.rmtree(settings.runs_dir, ignore_errors=True)
    page = await client.get(url, headers=auth)
    assert page.status_code == 200 and "parse_duration" in await page.get_data(as_text=True)
    await process_run(app.extensions["orchestrator"], run_id)  # restores the shard from its archive
    async with app.extensions["db"].session() as session:
        again = await session.scalar(select(Run).where(Run.id == run_id))
        assert again is not None and again.status == "completed" and again.verdict == verdict
    assert (settings.runs_dir / run_id / "upload" / "shard-0" / "upload.json").is_file()

    await asyncio.to_thread(purge_repo_files, settings, repo.id, [run_id])
    assert [info.key for info in blobs.list()] == []
