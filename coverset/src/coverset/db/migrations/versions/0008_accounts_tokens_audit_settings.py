"""accounts, api tokens, repo access, audit log, scoped settings

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-13 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from coverset.db.types import UTCDateTime

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("forge", sa.Enum("github", "fake", "local", native_enum=False, length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("login", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.String(length=1024), nullable=True),
        sa.Column("role", sa.Enum("admin", "member", native_enum=False, length=32), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("oauth_token", sa.String(length=2048), nullable=True),
        sa.Column("oauth_refresh_token", sa.String(length=2048), nullable=True),
        sa.Column("oauth_expires_at", UTCDateTime(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_login_at", UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("forge", "external_id", name="uq_users_forge_external"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_login"), ["login"], unique=False)

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.Enum("personal", "repo", native_enum=False, length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("repo_id", sa.Integer(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=True),
        sa.Column("last_used_at", UTCDateTime(), nullable=True),
        sa.Column("revoked_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_api_tokens_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_api_tokens_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_api_tokens_repo_id"), ["repo_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_api_tokens_user_id"), ["user_id"], unique=False)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.Column(
            "actor_kind",
            sa.Enum("user", "admin_token", "api_token", "oidc", "system", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("actor_label", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("repo_id", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_audit_log_repo_id_repos"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_audit_log_action"), ["action"], unique=False)
        batch_op.create_index(batch_op.f("ix_audit_log_at"), ["at"], unique=False)
        batch_op.create_index(batch_op.f("ix_audit_log_repo_id"), ["repo_id"], unique=False)

    op.create_table(
        "repo_access",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column(
            "permission", sa.Enum("read", "write", "admin", native_enum=False, length=32), nullable=False
        ),
        sa.Column("checked_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_repo_access_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_repo_access_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "repo_id", name=op.f("pk_repo_access")),
    )
    with op.batch_alter_table("repo_access", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_repo_access_repo_id"), ["repo_id"], unique=False)

    op.create_table(
        "scoped_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "scope_kind",
            sa.Enum("instance", "org", "repo", "user", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_scoped_settings_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scoped_settings")),
        sa.UniqueConstraint("scope_kind", "scope_id", "key", name="uq_scoped_settings"),
    )
    with op.batch_alter_table("scoped_settings", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_scoped_settings_updated_by"), ["updated_by"], unique=False)

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("csrf", sa.String(length=128), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
    )
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_sessions_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_sessions_user_id"))

    op.drop_table("user_sessions")
    with op.batch_alter_table("scoped_settings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scoped_settings_updated_by"))

    op.drop_table("scoped_settings")
    with op.batch_alter_table("repo_access", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_repo_access_repo_id"))

    op.drop_table("repo_access")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_log_repo_id"))
        batch_op.drop_index(batch_op.f("ix_audit_log_at"))
        batch_op.drop_index(batch_op.f("ix_audit_log_action"))

    op.drop_table("audit_log")
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_tokens_user_id"))
        batch_op.drop_index(batch_op.f("ix_api_tokens_repo_id"))

    op.drop_table("api_tokens")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_login"))

    op.drop_table("users")
