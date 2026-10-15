"""integer ids are BIGINT on Postgres (SQLite unchanged)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-14 09:00:00.000000

Every integer primary key, and every foreign key that points at one, becomes ``BIGINT`` on
Postgres, and each ``SERIAL`` sequence counts in ``bigint``: a Postgres ``SERIAL`` is 32-bit, ids are
never reused, and the web layer accepts ids up to 2**63 - 1 (``coverset.db.types.BigIntId``). On
SQLite this revision does nothing: ``INTEGER`` is already 64-bit there, and only ``INTEGER PRIMARY
KEY`` is the auto-incrementing rowid alias, so the models keep ``INTEGER`` on SQLite.

Each table is altered in one statement (one rewrite per table, not one per column).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_COLUMNS: dict[str, tuple[str, ...]] = {
    "repos": ("id",),
    "users": ("id",),
    "executor_spend": ("id",),
    "commits": ("id", "repo_id"),
    "invariants": ("id", "repo_id"),
    "pull_requests": ("id", "repo_id"),
    "snapshots": ("id", "repo_id"),
    "tests": ("id", "repo_id"),
    "runs": ("repo_id", "pr_id", "snapshot_id"),
    "run_events": ("id",),
    "webhook_deliveries": ("id",),
    "forge_calls": ("id", "repo_id"),
    "review_findings": ("id",),
    "invariant_results": ("invariant_id",),
    "test_results": ("test_id",),
    "test_attempts": ("test_id",),
    "test_stats": ("repo_id", "test_id"),
    "api_tokens": ("id", "user_id", "repo_id"),
    "audit_log": ("id", "repo_id"),
    "repo_access": ("user_id", "repo_id"),
    "scoped_settings": ("id", "updated_by"),
    "user_sessions": ("user_id",),
}
"""Table -> its integer key columns: the ``id`` primary keys and the foreign keys pointing at one."""


def _retype(target: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, columns in ID_COLUMNS.items():
        clauses = ", ".join(f'ALTER COLUMN "{column}" TYPE {target}' for column in columns)
        op.execute(f'ALTER TABLE "{table}" {clauses}')
        if "id" in columns:
            sequence = bind.exec_driver_sql(f"SELECT pg_get_serial_sequence('{table}', 'id')").scalar()
            if sequence:
                op.execute(f"ALTER SEQUENCE {sequence} AS {target}")


def upgrade() -> None:
    """Upgrade schema."""
    _retype("bigint")


def downgrade() -> None:
    """Downgrade schema."""
    _retype("integer")
