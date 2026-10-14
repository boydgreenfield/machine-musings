"""repos.ai_review_enabled becomes a repository-scope policy.ai_review row

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-14 22:00:00.000000

The per-repository opt-in predates scoped settings, which resolved it as an implicit repository
value of ``policy.ai_review``. Every repository that opted in gets that value as an explicit
repository row (unless it already has one, which always won), and the column goes. The downgrade
restores the column as false: the rows stay and keep deciding.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        INSERT INTO scoped_settings (scope_kind, scope_id, key, value, updated_at)
        SELECT 'repo', CAST(r.id AS VARCHAR), 'policy.ai_review', '{"v": true}', now()
        FROM repos r
        WHERE r.ai_review_enabled AND NOT EXISTS (
            SELECT 1 FROM scoped_settings s
            WHERE s.scope_kind = 'repo' AND s.scope_id = CAST(r.id AS VARCHAR) AND s.key = 'policy.ai_review'
        )
        """
    )
    op.drop_column("repos", "ai_review_enabled")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "repos", sa.Column("ai_review_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
    )
