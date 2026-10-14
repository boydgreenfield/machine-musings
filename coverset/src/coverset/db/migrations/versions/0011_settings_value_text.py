"""settings.value as text, for the vault's sealed GitHub App

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-14 20:00:00.000000

``/setup`` stores the GitHub App's credentials (ids, client secret, webhook secret and the PEM private
key) as one encrypted row of ``settings`` (``coverset.db.vault``). The sealed, base64-encoded value
of a 2048-bit RSA key plus the rest is about 2.7 KB, more than the ``VARCHAR(2048)`` the column had.
``VARCHAR(n)`` to ``TEXT`` is a catalog-only change on Postgres (no table rewrite); the downgrade
fails if a longer value is stored, so forget the App (``/setup``) before downgrading.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("settings", "value", existing_type=sa.String(length=2048), type_=sa.Text())


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("settings", "value", existing_type=sa.Text(), type_=sa.String(length=2048))
