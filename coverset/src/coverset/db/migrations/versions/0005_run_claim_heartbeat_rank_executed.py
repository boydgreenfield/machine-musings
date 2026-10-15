"""run claimed_by, heartbeat_at and rank_executed

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from coverset.db.types import UTCDateTime

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("claimed_by", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("heartbeat_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("rank_executed", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("rank_executed")
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("claimed_by")
