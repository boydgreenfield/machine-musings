"""webhook body digest, run approval_requested_by, pull request author

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("body_sha256", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint(batch_op.f("uq_webhook_deliveries_body_sha256"), ["body_sha256"])
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("approval_requested_by", sa.String(length=255), nullable=True))
    with op.batch_alter_table("pull_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("author", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("pull_requests", schema=None) as batch_op:
        batch_op.drop_column("author")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("approval_requested_by")
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_webhook_deliveries_body_sha256"), type_="unique")
        batch_op.drop_column("body_sha256")
