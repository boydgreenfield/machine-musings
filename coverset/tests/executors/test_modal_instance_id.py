"""The installation's instance id: one ``instance_id`` row of ``settings``, the same for every process,
container and replica, so the reaper finds sandboxes an earlier container created."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from coverset.config import Settings
from coverset.db.models import AppSetting
from coverset.db.session import Database
from coverset.executors.base import ExecutorError
from coverset.executors.modal_ import INSTANCE_ROW, ModalExecutor, SandboxSlots, instance_id


async def _rows(settings: Settings) -> list[tuple[str, str]]:
    database = Database.from_url(settings.database_url)
    try:
        async with database.session() as session:
            result = await session.execute(select(AppSetting.key, AppSetting.value).order_by(AppSetting.key))
            return [(key, value) for key, value in result.all()]
    finally:
        await database.engine.dispose()


async def test_the_first_call_creates_the_row(settings: Settings) -> None:
    assert await _rows(settings) == []
    database = Database.from_url(settings.database_url)
    try:
        async with database.session() as session:
            first = await instance_id(session)
        async with database.session() as session:
            again = await instance_id(session)
    finally:
        await database.engine.dispose()
    assert str(uuid.UUID(first)) == first and again == first
    assert await _rows(settings) == [(INSTANCE_ROW, first)]


async def test_concurrent_creators_on_two_connections_agree(settings: Settings) -> None:
    """Each proposes its own UUID; ``ON CONFLICT DO NOTHING`` keeps the first and both read it back."""
    database = Database.from_url(settings.database_url)
    try:
        async with database.session() as one, database.session() as two:
            await one.connection()  # two separate connections, both open before either inserts
            await two.connection()
            found = await asyncio.gather(instance_id(one), instance_id(two))
        results = await asyncio.gather(*(_fresh_call(database) for _ in range(8)))
    finally:
        await database.engine.dispose()
    assert len(set(found)) == 1 and set(results) == set(found)
    assert await _rows(settings) == [(INSTANCE_ROW, found[0])]


async def _fresh_call(database: Database) -> str:
    async with database.session() as session:
        return await instance_id(session)


async def test_the_id_is_stable_across_restarts(settings: Settings, fake_modal: Any) -> None:
    """A new executor (a restarted container, the ``coverset modal reap`` CLI) reads the same id,
    so a plain reap finds the sandboxes the earlier one tagged; the id is cached per executor."""
    first = ModalExecutor(settings, client_factory=lambda: fake_modal, slots=SandboxSlots(4))
    try:
        mine = await first.instance()
    finally:
        await first.close()
    restarted = ModalExecutor(settings, client_factory=lambda: fake_modal, slots=SandboxSlots(4))
    try:
        assert await restarted.instance() == mine
        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:  # changing the row does not move a live executor
                row = await session.get(AppSetting, INSTANCE_ROW)
                assert row is not None
                row.value = str(uuid.uuid4())
                await session.commit()
        finally:
            await database.engine.dispose()
        assert await restarted.instance() == mine
    finally:
        await restarted.close()


async def test_no_database_means_no_untagged_sandbox(tmp_path: Any, fake_modal: Any, world: Any) -> None:
    """Without its id the executor creates nothing: an untagged sandbox could never be reaped."""
    settings = Settings.for_tests(
        data_dir=tmp_path / "data", database_url="postgresql+asyncpg://nobody@127.0.0.1:9/none"
    )
    executor = ModalExecutor(settings, client_factory=lambda: fake_modal, slots=SandboxSlots(4))
    try:
        with pytest.raises(ExecutorError, match="instance id"):
            await executor.instance()
        image = fake_modal.Image.debian_slim("3.13")
        with pytest.raises(ExecutorError, match="instance id"):
            await executor.create_sandbox(image, role="smoke")
        assert world.create_calls == []
    finally:
        await executor.close()


def test_the_executor_keeps_no_file(settings: Settings, fake_modal: Any) -> None:
    ModalExecutor(settings, client_factory=lambda: fake_modal, slots=SandboxSlots(4))
    assert not (settings.data_dir / "instance_id").exists()
