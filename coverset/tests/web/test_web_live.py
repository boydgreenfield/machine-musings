"""A run that is not finished: approval state, the live SSE connection and its end."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import delete
from test_web_sse import frames
from web_helpers import Demo

from coverset.db.models import Run, RunEvent


@pytest.fixture
async def pending_run(app: Quart, demo: Demo) -> AsyncIterator[str]:
    """An untrusted PR run awaiting approval, with one queued event; removed afterwards."""
    db = app.extensions["db"]
    settings = app.extensions["settings"]
    now = settings.now()
    async with db.session() as session:
        run = Run(
            repo_id=demo.repo_id,
            head_sha="f" * 40,
            base_sha="e" * 40,
            branch="untrusted",
            trigger="pull_request",
            status="awaiting_approval",
            trusted=False,
            approval_requested_by="mallory",
            timeline={"queued": now.isoformat()},
        )
        session.add(run)
        await session.flush()
        session.add(
            RunEvent(run_id=run.id, seq=1, kind="queued", payload={}, at=now + timedelta(seconds=0.1))
        )
        await session.commit()
        run_id = run.id
    try:
        yield run_id
    finally:
        async with db.session() as session:
            await session.execute(delete(RunEvent).where(RunEvent.run_id == run_id))
            await session.execute(delete(Run).where(Run.id == run_id))
            await session.commit()


async def test_awaiting_approval_shows_the_approve_button(client: Client, pending_run: str) -> None:
    html = await (await client.get(f"/runs/{pending_run}")).get_data(as_text=True)
    assert "awaiting approval" in html and "Approve run" in html
    assert f'action="/runs/{pending_run}/approve"' in html and 'name="csrf_token"' in html
    assert f'sse-connect="/runs/{pending_run}/events?after=1"' in html
    dashboard = await (await client.get("/")).get_data(as_text=True)
    assert "s-wait" in dashboard and f"/runs/{pending_run}" in dashboard


async def test_live_stream_ends_when_the_run_reaches_a_terminal_status(
    app: Quart, client: Client, pending_run: str
) -> None:
    db = app.extensions["db"]

    async def finish_later() -> None:
        await asyncio.sleep(0.8)
        async with db.session() as session:
            run = await session.get(Run, pending_run)
            assert run is not None
            run.status = "cancelled"
            await session.commit()

    response, _ = await asyncio.gather(client.get(f"/runs/{pending_run}/events?after=1"), finish_later())
    events = frames(await response.get_data(as_text=True))
    assert events[-1][1] == "done"
    assert {"head", "timeline", "gantt", "failures"} <= {e for _, e, _ in events}
