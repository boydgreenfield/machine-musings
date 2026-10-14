"""``planner.run_transaction`` retries transient failures on both backends: SQLite's busy error and
Postgres's deadlock and serialization failures (which arrive as a plain ``DBAPIError`` carrying the
SQLSTATE), and nothing else."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from coverset.db.session import Database
from coverset.orchestrator.planner import is_retryable, run_transaction


class _Orig(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


def _error(cls: type[DBAPIError], sqlstate: str | None) -> DBAPIError:
    return cls("UPDATE ...", {}, _Orig(sqlstate))


class _FakeDb:
    """Only what ``run_transaction`` uses: ``session()`` yielding something with ``commit()``."""

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        class Session:
            async def commit(self) -> None:
                return None

        yield Session()


@pytest.mark.parametrize(
    ("error", "retried"),
    [
        (_error(DBAPIError, "40P01"), True),  # deadlock detected
        (_error(DBAPIError, "40001"), True),  # serialization failure
        (_error(OperationalError, None), True),  # SQLite: database is locked
        (_error(DBAPIError, "57014"), False),  # statement timeout: would only time out again
        (_error(IntegrityError, "23505"), False),  # a unique violation is an answer, not a blip
        (_error(DBAPIError, None), False),
    ],
)
async def test_which_failures_are_retried(error: DBAPIError, retried: bool) -> None:
    assert is_retryable(error) is retried
    calls: list[int] = []

    async def work(_session: Any) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise error
        return "done"

    if retried:
        assert await run_transaction(_FakeDb(), work, delay_s=0) == "done"  # type: ignore[arg-type]
        assert len(calls) == 2
    else:
        with pytest.raises(type(error)):
            await run_transaction(_FakeDb(), work, delay_s=0)  # type: ignore[arg-type]
        assert len(calls) == 1


async def test_the_last_attempt_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def work(_session: Any) -> None:
        raise _error(DBAPIError, "40P01")

    with pytest.raises(DBAPIError):
        await run_transaction(_FakeDb(), work, attempts=3, delay_s=0)  # type: ignore[arg-type]


async def test_a_real_postgres_deadlock_is_retried(postgres_url: str) -> None:
    """Two transactions take the same two row locks in opposite orders: Postgres kills one with
    ``deadlock detected`` and ``run_transaction`` runs it again, so both finish."""
    db = Database.from_url(postgres_url)
    try:
        async with db.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE deadlock_probe (id int PRIMARY KEY, n int NOT NULL)"))
            await connection.execute(text("INSERT INTO deadlock_probe VALUES (1, 0), (2, 0)"))
        both_hold_one = asyncio.Barrier(2)
        attempts: list[str] = []

        def work(first: int, second: int, name: str) -> Any:
            async def body(session: Any) -> str:
                attempts.append(name)
                await session.execute(
                    text("UPDATE deadlock_probe SET n = n + 1 WHERE id = :id"), {"id": first}
                )
                if attempts.count(name) == 1:  # only the first attempts meet: that is the deadlock
                    await asyncio.wait_for(both_hold_one.wait(), 10)
                await session.execute(
                    text("UPDATE deadlock_probe SET n = n + 1 WHERE id = :id"), {"id": second}
                )
                return name

            return body

        results = await asyncio.gather(
            run_transaction(db, work(1, 2, "a"), delay_s=0.05),
            run_transaction(db, work(2, 1, "b"), delay_s=0.05),
        )
        assert sorted(results) == ["a", "b"]
        assert len(attempts) == 3, attempts  # one of the two ran twice
        async with db.engine.connect() as connection:
            rows = (await connection.execute(text("SELECT n FROM deadlock_probe ORDER BY id"))).all()
        assert [n for (n,) in rows] == [2, 2]  # the killed attempt's updates were rolled back
    finally:
        await db.engine.dispose()
