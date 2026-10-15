"""run extra_env, retry_increment_keys and summary_json

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("extra_env", sa.JSON(), nullable=False, server_default="{}"))
        batch_op.add_column(sa.Column("retry_increment_keys", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("summary_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("summary_json")
        batch_op.drop_column("retry_increment_keys")
        batch_op.drop_column("extra_env")
