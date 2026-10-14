"""Copy a control-plane database to another backend: ``coverset db copy --to URL``.

The usual move is a laptop's or a VM's SQLite file into a hosted Postgres; the reverse (a Postgres
database into a SQLite file, to look at production data locally) works the same way. Both sides go
through SQLAlchemy Core with the models' column types, so every value is converted the way the app
itself would read and write it: aware UTC datetimes (naive text in SQLite, ``timestamptz`` in
Postgres), JSON, booleans, ``bytea`` digests.

Steps (:func:`copy_database`):

1. The source must be at the packaged Alembic head (``coverset init-db`` first); the target is
   migrated to head and must hold no rows in any coverset table.
2. Tables are copied in dependency order (``Base.metadata.sorted_tables``, parents first), each in
   primary-key order and in batches of ``batch_size`` rows, all inside one transaction on the
   target: a failure leaves it empty. A column that references its own table
   (``runs.scoring_basis_run_id``) is inserted as NULL and set afterwards, so row order never
   matters.
3. Postgres sequences are moved past the copied ids (``setval``), so new rows do not collide.
4. Every table's row count is compared with the source; a difference is an error.

Facts, mirrors and uploads are files (or objects), not rows: they move separately.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Table, bindparam, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from coverset.db.models import Base
from coverset.db.session import current_revisions, head_revisions, make_engine, migrate

DEFAULT_BATCH_SIZE = 1000

Progress = Callable[[str, int], None]
"""``progress(table, rows_copied_so_far)``, called after every batch."""


class CopyError(RuntimeError):
    """The copy cannot start (source behind head, target not empty) or did not verify."""


@dataclass
class CopyReport:
    """Rows per table on each side after a copy (equal when it verified)."""

    source: dict[str, int] = field(default_factory=dict)
    target: dict[str, int] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)
    """Migrations applied to the target first."""

    @property
    def total(self) -> int:
        """Rows copied in all."""
        return sum(self.target.values())

    @property
    def mismatched(self) -> list[str]:
        """Tables whose row counts differ."""
        return [name for name, count in self.source.items() if self.target.get(name) != count]


def _tables() -> list[Table]:
    return list(Base.metadata.sorted_tables)


def _self_references(table: Table) -> list[str]:
    """Columns of ``table`` that are foreign keys to ``table`` itself."""
    return sorted({fk.parent.name for fk in table.foreign_keys if fk.column.table is table})


async def _counts(connection: AsyncConnection) -> dict[str, int]:
    return {
        table.name: int(await connection.scalar(select(func.count()).select_from(table)) or 0)
        for table in _tables()
    }


async def _copy_table(
    source: AsyncConnection, target: AsyncConnection, table: Table, batch_size: int, progress: Progress | None
) -> int:
    deferred = _self_references(table)
    order = list(table.primary_key.columns)
    copied = 0
    later: list[dict[str, Any]] = []
    result = await source.stream(select(table).order_by(*order).execution_options(yield_per=batch_size))
    async for partition in result.mappings().partitions(batch_size):
        rows = [dict(row) for row in partition]
        if deferred:
            for row in rows:
                values = {column: row[column] for column in deferred if row[column] is not None}
                if values:
                    later.append({**{f"pk_{c.name}": row[c.name] for c in order}, **values})
                for column in deferred:
                    row[column] = None
        try:
            await target.execute(table.insert(), rows)
        except Exception as exc:
            raise CopyError(f"copying {table.name} failed after {copied} rows: {exc}") from exc
        copied += len(rows)
        if progress is not None:
            progress(table.name, copied)
    for column in deferred:
        updates = [row for row in later if column in row]
        if not updates:
            continue
        where = [c == bindparam(f"pk_{c.name}") for c in order]
        statement = table.update().where(*where).values({column: bindparam(column)})
        for start in range(0, len(updates), batch_size):
            batch = [
                {key: row[key] for key in (*(f"pk_{c.name}" for c in order), column)}
                for row in updates[start : start + batch_size]
            ]
            await target.execute(statement, batch)
    return copied


async def _reset_sequences(target: AsyncConnection) -> None:
    """Move every ``SERIAL`` sequence past the largest copied id (Postgres only)."""
    for table in _tables():
        if (
            "id" not in table.columns
            or not table.columns["id"].autoincrement
            or len(table.primary_key.columns) != 1
        ):
            continue
        sequence = await target.scalar(
            text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": table.name}
        )
        if sequence:
            await target.execute(
                text(
                    f'SELECT setval(:sequence, COALESCE((SELECT max(id) FROM "{table.name}"), 0) + 1, false)'
                ),
                {"sequence": sequence},
            )


async def copy_database(
    source_url: str,
    target_url: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: Progress | None = None,
) -> CopyReport:
    """Copy every coverset table from ``source_url`` into ``target_url`` (see the module docstring)."""
    if make_url(source_url) == make_url(target_url):
        raise CopyError("the source and the target are the same database")
    source = make_engine(source_url)
    target = make_engine(target_url)
    try:
        return await _copy(source, target, batch_size, progress)
    finally:
        await source.dispose()
        await target.dispose()


async def _copy(
    source: AsyncEngine, target: AsyncEngine, batch_size: int, progress: Progress | None
) -> CopyReport:
    current = set(await current_revisions(source))
    if current != head_revisions():
        found = ", ".join(sorted(current)) or "no schema"
        raise CopyError(
            f"the source is at {found}, not the latest revision {', '.join(sorted(head_revisions()))}: "
            "run `coverset init-db` against it first"
        )
    report = CopyReport(applied=await migrate(target))
    async with target.connect() as connection:
        occupied = {name: count for name, count in (await _counts(connection)).items() if count}
    if occupied:
        listed = ", ".join(f"{name} ({count})" for name, count in sorted(occupied.items()))
        raise CopyError(f"the target already holds rows: {listed}; copy into an empty database")
    async with source.connect() as reader, target.begin() as writer:
        report.source = await _counts(reader)
        for table in _tables():
            await _copy_table(reader, writer, table, batch_size, progress)
        if target.dialect.name == "postgresql":
            await _reset_sequences(writer)
        report.target = await _counts(writer)
        if report.mismatched:
            raise CopyError(f"row counts differ after the copy in: {', '.join(report.mismatched)}")
    return report
