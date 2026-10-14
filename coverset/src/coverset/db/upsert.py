"""``INSERT ... ON CONFLICT`` for SQLite and Postgres through one call.

Both backends spell an upsert the same way (``INSERT ... ON CONFLICT (keys) DO UPDATE SET col =
excluded.col``), but SQLAlchemy builds it from each dialect's own ``insert`` construct. Callers that
used to pick one (``orchestrator/history.py`` for ``test_stats``, the Modal executor for ``envs``)
call :func:`upsert` instead, so nothing above the database layer names a dialect.

The statement is atomic on both: on Postgres a concurrent insert of the same key under READ
COMMITTED waits for the other transaction and then updates its row instead of raising a unique
violation, which is what makes it safe for two replicas (or two runs) recording the same row.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["upsert"]


def _insert_for(dialect: str) -> Any:
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        return insert
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        return insert
    return None


def _table_of(target: Any) -> Table:
    table = getattr(target, "__table__", target)
    if not isinstance(table, Table):
        raise TypeError(f"upsert needs a mapped class or a Table, got {target!r}")
    return table


async def upsert(
    session: AsyncSession,
    target: Any,
    rows: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
    update_columns: Sequence[str] | None = None,
) -> None:
    """Insert ``rows`` into ``target`` (a mapped class or a :class:`~sqlalchemy.Table`); where a row's
    ``keys`` already exist, overwrite ``update_columns`` (default: every non-key column the row
    carries) with the row's values instead. With nothing to update the conflict is ignored.

    Rows are grouped by their set of columns and each group is one multi-row statement, its rows
    sorted by ``keys``: two transactions upserting overlapping rows then lock them in the same
    order, so on Postgres they wait for each other instead of deadlocking. On a
    dialect other than SQLite and Postgres the rows are merged one by one through the ORM (only
    for mapped classes), which is correct but neither atomic nor fast.
    """
    if not rows:
        return
    table = _table_of(target)
    insert = _insert_for(session.get_bind().dialect.name)
    if insert is None:  # pragma: no cover - coverset runs on SQLite and Postgres only
        if table is target:
            raise NotImplementedError(f"upsert into a bare Table on {session.get_bind().dialect.name}")
        for row in rows:
            await session.merge(target(**row))
        await session.flush()
        return
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(sorted(row))].append(row)
    for columns, group in groups.items():
        statement = insert(table)
        wanted = columns if update_columns is None else tuple(update_columns)
        changed = [column for column in wanted if column not in keys and column in columns]
        statement = (
            statement.on_conflict_do_update(
                index_elements=list(keys), set_={column: statement.excluded[column] for column in changed}
            )
            if changed
            else statement.on_conflict_do_nothing(index_elements=list(keys))
        )
        ordered = sorted(group, key=lambda row: tuple(row[key] for key in keys))
        await session.execute(statement, ordered)
