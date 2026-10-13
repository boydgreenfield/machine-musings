"""tests.nodeid holds up to 4,096 characters (parametrised pytest ids can run long)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-13 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("tests") as batch:
        batch.alter_column("nodeid", existing_type=sa.String(length=1024), type_=sa.String(length=4096))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("tests") as batch:
        batch.alter_column("nodeid", existing_type=sa.String(length=4096), type_=sa.String(length=1024))
