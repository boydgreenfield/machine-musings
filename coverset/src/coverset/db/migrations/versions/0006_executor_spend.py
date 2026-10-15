"""executor_spend: spend that belongs to no run (pool idle, snapshots, smoke, image builds)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from coverset.db.types import UTCDateTime

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "executor_spend",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("executor", sa.Enum("local", "modal", native_enum=False, length=32), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("pool", "snapshot", "smoke", "build", "bench", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("usd", sa.Float(), nullable=False),
        sa.Column("sandbox_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_executor_spend")),
    )
    with op.batch_alter_table("executor_spend", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_executor_spend_day_executor_kind"), ["day", "executor", "kind"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("executor_spend", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_executor_spend_day_executor_kind"))

    op.drop_table("executor_spend")
