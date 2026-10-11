"""Control-plane configuration: every ``COVERSET_*`` key, typed and validated.

Values come from (highest precedence first) constructor arguments, the process environment, and a
``.env`` file in the working directory. Secrets are :class:`pydantic.SecretStr` so they never appear
in ``repr`` output; :func:`coverset.logging.configure_logging` registers their live values with the
log redactor.
"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

MIB = 1024 * 1024
MIN_ADMIN_TOKEN_CHARS = 32
MIN_WEBHOOK_SECRET_CHARS = 16
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

CommaList = Annotated[list[str], NoDecode]
"""A ``list[str]`` field that also accepts a comma-separated (or JSON array) string from the env."""

Env = Literal["dev", "test", "prod"]
AiMode = Literal["off", "fake", "anthropic"]
ExecutorKind = Literal["local", "modal"]
UntrustedPrPolicy = Literal["approve", "run_blocked_network", "skip"]
SandboxNetwork = Literal["blocked", "allowlist", "open"]


def _default_workers() -> int:
    return min(8, os.cpu_count() or 1)


def split_bind(bind: str) -> tuple[str, int]:
    """Split a ``host:port`` bind string (IPv6 hosts in brackets) into host and port."""
    host, sep, port = bind.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"bind must look like host:port, got {bind!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, int(port)


def is_loopback_host(host: str) -> bool:
    """Return True for ``localhost`` and any loopback IP literal (127.0.0.0/8, ::1)."""
    if host.lower() in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    """All control-plane settings. Field ``foo`` is read from ``COVERSET_FOO``."""

    model_config = SettingsConfigDict(
        env_prefix="COVERSET_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    env: Env = "dev"
    bind: str = "127.0.0.1:8000"
    public_url: str = Field(default="", description="External base URL; empty means http://<bind>.")
    admin_token: SecretStr | None = None
    data_dir: Path = Path("data")
    database_url: str = Field(default="", description="SQLAlchemy URL; empty means <data_dir>/coverset.db.")

    github_webhook_secret: SecretStr | None = None
    github_app_id: int | None = None
    github_app_client_id: str | None = None
    github_app_client_secret: SecretStr | None = None
    github_app_private_key_path: Path = Path("secrets/github-app.pem")
    github_token: SecretStr | None = None
    allowed_owners: CommaList = Field(default_factory=list)
    allowed_repos: CommaList = Field(default_factory=list)
    untrusted_pr_policy: UntrustedPrPolicy = "approve"
    run_drafts: bool = False

    ai: AiMode = "fake"
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key")
    )
    ai_review_untrusted: bool = False
    ai_max_bundle_chars: int = Field(default=120_000, gt=0)
    ai_exclude_globs: CommaList = Field(default_factory=list)

    executor: ExecutorKind = "local"
    local_max_workers: int = Field(default_factory=_default_workers, gt=0)
    local_allow_webhook_runs: bool = False
    modal_environment: str = "coverset"
    shard_cpu_limit: float = Field(default=2.0, gt=0)
    shard_memory_mib: int = Field(default=2048, gt=0)
    run_timeout_s: int = Field(default=1800, gt=0)
    pool_ttl_s: int = Field(default=600, ge=0)
    pool_size: int = Field(default=4, ge=0)
    max_shards: int = Field(default=32, gt=0)
    max_sandboxes: int = Field(default=48, gt=0)
    max_concurrent_runs: int = Field(default=4, gt=0)
    retry_budget_per_run: int = Field(default=5, ge=0)
    cancel_superseded: bool = True
    modal_daily_budget_usd: float = Field(default=5.0, ge=0)
    paused: bool = False
    sandbox_network: SandboxNetwork = "blocked"
    sandbox_domain_allowlist: CommaList = Field(default_factory=list)
    requeue_on_boot: bool = False

    max_bundle_bytes: int = Field(default=200 * MIB, gt=0)
    max_stdout_bytes: int = Field(default=50 * MIB, gt=0)
    oidc_audience: str = Field(default="", description="OIDC audience; empty means public_url.")
    public_verdict_rpm: int = Field(default=60, ge=0)

    retention_runs_days: int = Field(default=30, gt=0)
    retention_deliveries_days: int = Field(default=14, gt=0)
    retention_events_days: int = Field(default=7, gt=0)

    fake_now: datetime | None = None
    skip_migrations: bool = False

    @field_validator(
        "allowed_owners", "allowed_repos", "ai_exclude_globs", "sandbox_domain_allowlist", mode="before"
    )
    @classmethod
    def _split_comma_list(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        return [item.strip() for item in text.split(",") if item.strip()]

    @field_validator("bind")
    @classmethod
    def _check_bind(cls, value: str) -> str:
        split_bind(value)
        return value

    @field_validator("fake_now")
    @classmethod
    def _aware_fake_now(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def _derive_and_check(self) -> Self:
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{self.data_dir / 'coverset.db'}"
        if not self.public_url:
            host, port = split_bind(self.bind)
            netloc = f"[{host}]" if ":" in host else host
            self.public_url = f"http://{netloc}:{port}"
        self.public_url = self.public_url.rstrip("/")
        public = urlsplit(self.public_url)
        if public.scheme not in ("http", "https") or not public.hostname:
            raise ValueError(f"COVERSET_PUBLIC_URL must be an absolute http(s) URL, got {self.public_url!r}")
        if not self.oidc_audience:
            self.oidc_audience = self.public_url

        token = self.admin_token.get_secret_value() if self.admin_token else ""
        if self.admin_token is not None and len(token) < MIN_ADMIN_TOKEN_CHARS:
            raise ValueError(f"COVERSET_ADMIN_TOKEN must be at least {MIN_ADMIN_TOKEN_CHARS} characters")
        if not self.is_loopback and not token:
            raise ValueError(
                f"COVERSET_BIND={self.bind} is not a loopback address: set COVERSET_ADMIN_TOKEN "
                f"(at least {MIN_ADMIN_TOKEN_CHARS} characters) or bind to 127.0.0.1"
            )
        webhook_secret = self.github_webhook_secret  # empty means unset: webhooks then fail closed
        webhook_secret_chars = len(webhook_secret.get_secret_value()) if webhook_secret else 0
        if 0 < webhook_secret_chars < MIN_WEBHOOK_SECRET_CHARS:
            raise ValueError(
                f"COVERSET_GITHUB_WEBHOOK_SECRET must be at least {MIN_WEBHOOK_SECRET_CHARS} characters"
            )
        if self.github_app_id is not None and not self.allowed_owners:
            raise ValueError("COVERSET_ALLOWED_OWNERS is required when COVERSET_GITHUB_APP_ID is set")
        if self.ai == "anthropic" and not (
            self.anthropic_api_key and self.anthropic_api_key.get_secret_value()
        ):
            raise ValueError("COVERSET_AI=anthropic requires ANTHROPIC_API_KEY")
        audience_host = (urlsplit(self.oidc_audience).hostname or "").lower()
        if audience_host == "github.com" or audience_host.endswith(".github.com"):
            raise ValueError(
                "COVERSET_OIDC_AUDIENCE must not be a github.com URL (GitHub's default audience)"
            )
        return self

    @classmethod
    def for_tests(cls, **overrides: Any) -> Settings:
        """Build hermetic test settings: ``env=test``, no ``.env`` file and no process environment.

        ``data_dir`` defaults to a fresh temporary directory; any field may be overridden by name.
        """
        values: dict[str, Any] = {"env": "test"}
        if "data_dir" not in overrides:
            values["data_dir"] = Path(tempfile.mkdtemp(prefix="coverset-test-"))
        values.update(overrides)
        return _HermeticSettings(**values)

    @property
    def is_loopback(self) -> bool:
        """True when the server binds only to a loopback interface."""
        return is_loopback_host(split_bind(self.bind)[0])

    @property
    def bind_host(self) -> str:
        """The host part of :attr:`bind` (IPv6 without brackets)."""
        return split_bind(self.bind)[0]

    @property
    def bind_port(self) -> int:
        """The port part of :attr:`bind`."""
        return split_bind(self.bind)[1]

    @property
    def mirrors_dir(self) -> Path:
        """Bare git mirrors, one per repo: ``<data_dir>/mirrors/<repo_id>.git``."""
        return self.data_dir / "mirrors"

    @property
    def facts_dir(self) -> Path:
        """Immutable Parquet facts: ``<data_dir>/facts/repo=<id>/run=<id>/...``."""
        return self.data_dir / "facts"

    @property
    def runs_dir(self) -> Path:
        """Per-run scratch files (args files, pulled bundles): ``<data_dir>/runs/<run_id>``."""
        return self.data_dir / "runs"

    def now(self) -> datetime:
        """Current time as an aware UTC datetime, pinned to :attr:`fake_now` when set."""
        return self.fake_now if self.fake_now is not None else datetime.now(UTC)

    def secret_values(self) -> list[str]:
        """Live values of every set :class:`SecretStr` field, for log redaction."""
        values = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                values.append(value.get_secret_value())
        return values


class _HermeticSettings(Settings):
    """Settings built from constructor arguments only (used by :meth:`Settings.for_tests`)."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


@cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment and ``.env``."""
    return Settings()
