"""Control-plane configuration: every ``COVERSET_*`` key, typed and validated.

Values come from (highest precedence first) constructor arguments, the process environment, and a
``.env`` file in the working directory. Secrets are :class:`pydantic.SecretStr` so they never appear
in ``repr`` output; :func:`coverset.logging.configure_logging` registers their live values with the
log redactor.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

MIB = 1024 * 1024
PRIVATE_DIR_MODE = 0o700

log = logging.getLogger(__name__)
MIN_ADMIN_TOKEN_CHARS = 32
MIN_WEBHOOK_SECRET_CHARS = 16
MIN_GITHUB_TOKEN_CHARS = 16
"""GitHub tokens are 40+ characters; a short value is a mistake, and too short to redact safely."""
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_DATABASE_URL = "postgresql+asyncpg://localhost/coverset"
"""A local Postgres (Homebrew's accepts the current user without a password), database ``coverset``."""
DATABASE_URL_ENV = "COVERSET_DATABASE_URL"
"""The environment key of :attr:`Settings.database_url`, the one key :meth:`Settings.for_tests` reads."""

CACHE_SETTINGS: dict[str, str] = {
    "facts_cache_entries": "COVERSET_FACTS_CACHE_ENTRIES",
    "facts_memory_limit": "COVERSET_FACTS_MEMORY_LIMIT",
    "facts_threads": "COVERSET_FACTS_THREADS",
    "facts_result_entries": "COVERSET_FACTS_RESULT_ENTRIES",
    "run_facts_entries": "COVERSET_RUN_FACTS_ENTRIES",
    "run_facts_mb": "COVERSET_RUN_FACTS_MB",
    "source_cache_mb": "COVERSET_SOURCE_CACHE_MB",
    "facts_compact": "COVERSET_FACTS_COMPACT",
    "agent_contexts": "COVERSET_AGENT_CONTEXTS",
}
"""The cache knobs' fields and the environment names the facts, web and agent modules read
(:meth:`Settings.cache_overrides`; those modules keep the environment as their fallback)."""
DUCKDB_SIZE = r"^\d{1,6}(\.\d{1,3})?\s?(KB|MB|GB|TB|KiB|MiB|GiB|TiB)$"
"""A DuckDB ``memory_limit`` value (``512MB``, ``1.5GiB``): it is interpolated into a ``SET``."""

CommaList = Annotated[list[str], NoDecode]
"""A ``list[str]`` field that also accepts a comma-separated (or JSON array) string from the env."""

Env = Literal["dev", "test", "prod"]
AiMode = Literal["off", "fake", "anthropic"]
ExecutorKind = Literal["local", "modal"]
ForgeKind = Literal["auto", "github", "fake"]
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


SPECIAL_USE_SUFFIXES = (".localhost", ".test", ".invalid", ".example")
"""RFC 6761 special-use names: never resolvable on the public internet, so never a tunnel URL."""


def is_private_host(host: str | None) -> bool:
    """True for loopback hosts and RFC 6761 special-use names (nothing on the internet can reach them)."""
    name = (host or "").lower().rstrip(".")
    if not name:
        return False
    return is_loopback_host(name) or any(name.endswith(suffix) for suffix in SPECIAL_USE_SUFFIXES)


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
    admins: CommaList = Field(
        default_factory=list,
        description="GitHub logins that are instance admins once signed in (the admin token always is).",
    )
    session_days: int = Field(default=30, gt=0, le=365, description="Lifetime of a login session.")
    trust_proxy: bool = Field(
        default=False,
        description="Honour X-Forwarded-For/Proto from the immediate upstream (set behind Fly, Caddy, an ALB).",
    )
    secret_key: SecretStr | None = Field(
        default=None, description="Cookie signing key; empty means a key file under the data dir."
    )
    uploads_enabled: bool = Field(default=True, description="Accept bundles at POST /v1/bundles.")
    max_upload_bytes: int = Field(default=200 * MIB, gt=0, description="Largest accepted uploaded bundle.")
    data_dir: Path = Path("data")
    database_url: str = Field(
        default=DEFAULT_DATABASE_URL,
        description=(
            "The Postgres database: postgresql+asyncpg://[user[:password]@]host[:port]/name (a plain "
            "postgresql:// URL is accepted); created on first use when the server allows it."
        ),
    )
    blob_url: str = Field(
        default="",
        description=(
            "Where facts, uploads and uploaded sources are stored: empty means files under <data_dir>; "
            "s3://bucket/prefix?endpoint_url=...&region=... for any S3-compatible store (credentials from "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY or the instance role)."
        ),
    )
    cache_dir: Path | None = Field(
        default=None,
        description="Local cache of the blob store's files and the git mirrors; empty means <data_dir>/cache.",
    )

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
    scan_after_baseline: bool = True
    """Run ``coverset invariants scan`` in the background after a trusted, completed baseline of the
    default branch's tip (``COVERSET_SCAN_AFTER_BASELINE``)."""

    forge: ForgeKind = "auto"
    executor: ExecutorKind = "local"
    local_max_workers: int = Field(default_factory=_default_workers, gt=0)
    local_allow_webhook_runs: bool = False
    modal_environment: str = "coverset"
    shard_cpu_limit: float = Field(default=2.0, gt=0, le=16)
    shard_memory_mib: int = Field(default=2048, gt=0, le=65536)
    run_timeout_s: int = Field(default=1800, gt=0, le=7200)
    pool_ttl_s: int = Field(default=600, ge=0, le=86400)
    pool_size: int = Field(default=4, ge=0, le=32)
    max_shards: int = Field(default=32, gt=0, le=128)
    max_sandboxes: int = Field(default=48, gt=0, le=256)
    max_concurrent_runs: int = Field(default=4, gt=0)
    retry_budget_per_run: int = Field(default=5, ge=0)
    cancel_superseded: bool = True
    modal_daily_budget_usd: float = Field(default=5.0, ge=0, le=1000)
    max_run_cost_usd: float = Field(default=1.0, ge=0, le=1000)
    """Per-run Modal cost cap: the post-planning reservation and the live stop (at most the daily budget)."""
    max_image_builds_per_day: int = Field(default=20, ge=0, le=1000)
    """Modal env image builds allowed per UTC day (each one is also priced into the daily budget)."""
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

    # Caches (docs/data-model.md, "Caches"; docs/benchmarks.md sizes them). None means "not set
    # here": the module reads the environment, then uses its default (given in each description).
    facts_cache_entries: int | None = Field(
        default=None, gt=0, description="Cached DuckDB databases of the facts layer (default 16)."
    )
    facts_memory_limit: str | None = Field(
        default=None, description="DuckDB memory_limit of each cached database (default 512MB)."
    )
    facts_threads: int | None = Field(
        default=None, gt=0, description="DuckDB threads of each cached database (default min(4, CPUs))."
    )
    facts_result_entries: int | None = Field(
        default=None, gt=0, description="Memoised run-scoped query results (default 256)."
    )
    run_facts_entries: int | None = Field(
        default=None, gt=0, description="Runs whose indexed facts the web keeps (default 8)."
    )
    run_facts_mb: int | None = Field(
        default=None, gt=0, description="Estimated MB the web's cached run facts may hold (default 384)."
    )
    source_cache_mb: int | None = Field(
        default=None, gt=0, description="MB of source text the web keeps (default 64)."
    )
    facts_compact: bool | None = Field(
        default=None, description="Also write the compact test_file_bits layout at ingest (default false)."
    )
    agent_contexts: int | None = Field(
        default=None, gt=0, description="Run contexts the agent API and MCP server keep (default 3)."
    )

    fake_now: datetime | None = None
    skip_migrations: bool = False
    migrate_on_start: bool = True
    """``coverset serve`` migrates the database to head before it serves (``COVERSET_MIGRATE_ON_START``,
    what a container does at start). Every replica may: on Postgres the migration holds an advisory
    lock (``coverset:migrate``), so concurrent starts run one after the other and the later ones find
    the schema at head. False is for migrating once per deploy in a release step (Fly's
    ``release_command``, an ECS one-off task); it also leaves the app's own startup migration off
    (docs/deploy.md, "Migrations on deploy")."""
    dev_login: bool = False
    """Offer "Sign in as a local user" on the login page (``COVERSET_DEV_LOGIN``). Only honoured with
    ``COVERSET_ENV=dev``, open mode (no admin token), a loopback bind, a private public URL and the
    fake forge with no GitHub credentials (:func:`coverset.web.account.dev_login_problems`)."""

    @field_validator(
        "allowed_owners",
        "allowed_repos",
        "ai_exclude_globs",
        "sandbox_domain_allowlist",
        "admins",
        mode="before",
    )
    @classmethod
    def _split_comma_list(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        return [item.strip() for item in text.split(",") if item.strip()]

    @field_validator("facts_memory_limit")
    @classmethod
    def _check_memory_limit(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not re.fullmatch(DUCKDB_SIZE, value.strip(), re.IGNORECASE):
            raise ValueError("COVERSET_FACTS_MEMORY_LIMIT must be a size such as 512MB or 1.5GiB")
        return value.strip()

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
        if self.database_url.startswith(("postgresql://", "postgres://")):
            # Normalise the common form to the async driver so hosted-Postgres URLs paste straight in.
            self.database_url = "postgresql+asyncpg://" + self.database_url.split("://", 1)[1]
        if not self.database_url.startswith("postgresql+asyncpg://"):
            scheme = self.database_url.split("://", 1)[0] if "://" in self.database_url else "no scheme"
            raise ValueError(
                f"COVERSET_DATABASE_URL must be a Postgres URL for the asyncpg driver, got {scheme}: set "
                "it to postgresql://user:password@host:5432/name (or postgresql+asyncpg://...), or unset "
                f"it for {DEFAULT_DATABASE_URL} (docs/postgres.md)"
            )
        if self.blob_url and not self.blob_url.startswith("s3://"):
            raise ValueError("COVERSET_BLOB_URL must be empty (local files) or an s3://bucket[/prefix] URL")
        if self.cache_dir is None:
            self.cache_dir = self.data_dir / "cache"
        else:
            self.cache_dir = self.cache_dir.expanduser().resolve()
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
        if not token and not is_private_host(public.hostname):
            # A public URL means a tunnel or proxy forwards internet traffic to the loopback bind,
            # where open mode would treat every caller as the operator.
            raise ValueError(
                f"COVERSET_PUBLIC_URL={self.public_url} is reachable from outside this machine: set "
                f"COVERSET_ADMIN_TOKEN (at least {MIN_ADMIN_TOKEN_CHARS} characters) before exposing "
                "coverset through a tunnel or proxy"
            )
        webhook_secret = self.github_webhook_secret  # empty means unset: webhooks then fail closed
        webhook_secret_chars = len(webhook_secret.get_secret_value()) if webhook_secret else 0
        if 0 < webhook_secret_chars < MIN_WEBHOOK_SECRET_CHARS:
            raise ValueError(
                f"COVERSET_GITHUB_WEBHOOK_SECRET must be at least {MIN_WEBHOOK_SECRET_CHARS} characters"
            )
        github_token_chars = len(self.github_token.get_secret_value()) if self.github_token else 0
        if 0 < github_token_chars < MIN_GITHUB_TOKEN_CHARS:
            raise ValueError(f"COVERSET_GITHUB_TOKEN must be at least {MIN_GITHUB_TOKEN_CHARS} characters")
        if self.github_app_id is not None and not self.allowed_owners:
            raise ValueError("COVERSET_ALLOWED_OWNERS is required when COVERSET_GITHUB_APP_ID is set")
        if self.ai == "anthropic" and not (
            self.anthropic_api_key and self.anthropic_api_key.get_secret_value()
        ):
            raise ValueError("COVERSET_AI=anthropic requires ANTHROPIC_API_KEY")
        if self.max_run_cost_usd > self.modal_daily_budget_usd:
            if "max_run_cost_usd" in self.model_fields_set:
                raise ValueError(
                    f"COVERSET_MAX_RUN_COST_USD ({self.max_run_cost_usd:.2f}) must not exceed "
                    f"COVERSET_MODAL_DAILY_BUDGET_USD ({self.modal_daily_budget_usd:.2f})"
                )
            self.max_run_cost_usd = self.modal_daily_budget_usd  # the default follows a smaller budget
        audience_host = (urlsplit(self.oidc_audience).hostname or "").lower()
        if audience_host == "github.com" or audience_host.endswith(".github.com"):
            raise ValueError(
                "COVERSET_OIDC_AUDIENCE must not be a github.com URL (GitHub's default audience)"
            )
        return self

    @classmethod
    def for_tests(cls, **overrides: Any) -> Settings:
        """Build hermetic test settings: ``env=test``, no ``.env`` file and, with one exception, no
        process environment.

        ``data_dir`` defaults to a fresh temporary directory; any field may be overridden by name.
        The exception is ``database_url``: it defaults to ``COVERSET_DATABASE_URL`` when the process
        sets it, which the test suite points at a scratch database of its own (``tests/conftest.py``),
        so a test that forgets ``database_url`` can never reach a developer's ``coverset`` database.
        """
        values: dict[str, Any] = {"env": "test"}
        if "data_dir" not in overrides:
            values["data_dir"] = Path(tempfile.mkdtemp(prefix="coverset-test-"))
        if "database_url" not in overrides and os.environ.get(DATABASE_URL_ENV, "").strip():
            values["database_url"] = os.environ[DATABASE_URL_ENV].strip()
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
        """Bare git mirrors, one per repo: ``<cache_dir>/mirrors/<repo_id>.git`` (disposable: a
        missing mirror costs one clone, docs/object-store.md)."""
        assert self.cache_dir is not None  # set by the validator
        return self.cache_dir / "mirrors"

    @property
    def hosted(self) -> bool:
        """True when the blobs live in an object store (``COVERSET_BLOB_URL``): with the database in
        Postgres, nothing under the data dir then has to survive a restart."""
        return bool(self.blob_url)

    @property
    def facts_dir(self) -> Path:
        """The working copy of the Parquet facts that DuckDB reads: ``<cache_dir>/facts/repo=<id>/
        run=<id>/...``, a cache of the blob store's ``facts/`` (docs/object-store.md)."""
        assert self.cache_dir is not None  # set by the validator
        return self.cache_dir / "facts"

    @property
    def runs_dir(self) -> Path:
        """Per-run scratch files (args files, pulled bundles): ``<data_dir>/runs/<run_id>``."""
        return self.data_dir / "runs"

    def cache_overrides(self) -> dict[str, str]:
        """The cache knobs set here, by environment name (``coverset.facts.cache.configure`` takes
        them); knobs left unset are omitted, so the modules fall back to the environment."""
        out: dict[str, str] = {}
        for name, env_name in CACHE_SETTINGS.items():
            value = getattr(self, name)
            if value is not None:
                out[env_name] = ("1" if value else "0") if isinstance(value, bool) else str(value)
        return out

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


def secure_data_dirs(settings: Settings) -> None:
    """Create ``data_dir`` and ``cache_dir`` with mode 0700 and tighten existing ones to 0700, and
    create ``runs_dir`` inside the data directory (what marks a coverset data directory for
    ``coverset demo --fresh``).

    Both roots hold private source: the data directory is the local blob store (uploaded sources and
    upload archives) and holds the runs' unpacked uploads; the cache directory holds the git
    mirrors and the fetched uploaded sources. Mode 0700 on the two roots keeps other local users
    out of everything below them. A directory coverset cannot chmod (another owner) is left alone
    with a warning.
    """
    assert settings.cache_dir is not None  # set by the validator
    for path in (settings.data_dir, settings.cache_dir):
        path.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        _restrict(path, PRIVATE_DIR_MODE)
    settings.runs_dir.mkdir(exist_ok=True)


def _restrict(path: Path, mode: int) -> None:
    try:
        if stat.S_IMODE(path.stat().st_mode) != mode:
            os.chmod(path, mode)
    except OSError as exc:
        log.warning("cannot restrict %s to mode %o: %s", path, mode, exc)
