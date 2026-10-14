"""The retention gc runs on the web engine, whose Postgres connections carry a 30 s statement
timeout; its bulk deletes lift it for their transaction (``SET LOCAL``), so a backlog that takes
longer than 30 s to delete still shrinks instead of being cancelled every day."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db import session as db_session
from coverset.db.session import Database
from coverset.facts.store import FactsStore
from coverset.orchestrator import history


async def test_collect_garbage_lifts_the_statement_timeout_inside_its_transaction(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    real = db_session.lift_statement_timeout

    async def recording(session: AsyncSession) -> None:
        await real(session)
        shown = None
        if session.get_bind().dialect.name == "postgresql":
            shown = (await session.execute(text("SHOW statement_timeout"))).scalar()
        seen.append({"in_transaction": session.in_transaction(), "bind": session.get_bind(), "shown": shown})

    monkeypatch.setattr(history, "lift_statement_timeout", recording)
    store = FactsStore(settings.facts_dir)
    dry = await history.collect_garbage(db, settings, store, dry_run=True)
    assert seen == [] and dry.dry_run  # a dry run deletes nothing and lifts nothing
    report = await history.collect_garbage(db, settings, store)
    assert not report.skipped
    assert len(seen) == 1
    assert seen[0]["in_transaction"] and seen[0]["bind"] is db.engine.sync_engine
    if db.backend == "postgresql":
        assert seen[0]["shown"] == "0"
