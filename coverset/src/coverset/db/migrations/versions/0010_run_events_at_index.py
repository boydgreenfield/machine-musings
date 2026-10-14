"""an index on run_events.at for the retention gc

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-14 16:00:00.000000

The retention gc deletes run events older than ``COVERSET_RETENTION_EVENTS_DAYS`` with one
``DELETE FROM run_events WHERE at < cutoff``. ``run_events`` is the largest table and had no index
on ``at``, so that delete scanned the whole table once a day; on a hosted instance that scan
competed with every request for 30 s at a time. ``ix_run_events_at`` makes it an index range scan.
Same DDL on both backends (batch mode is a pass-through on Postgres and an in-place
``CREATE INDEX`` on SQLite).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_events_at"), ["at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_events_at"))
