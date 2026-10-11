"""Settings defaults, environment parsing and validators."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from coverset.config import Settings, get_settings

LONG_TOKEN = "x" * 32


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """No COVERSET_* / ANTHROPIC_API_KEY variables and no .env file in the working directory."""
    for name in list(os.environ):
        if name.startswith("COVERSET_") or name == "ANTHROPIC_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


@pytest.mark.security
def test_defaults(tmp_path: Path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path)
    assert settings.env == "test"
    assert settings.bind == "127.0.0.1:8000"
    assert settings.is_loopback
    assert settings.public_url == "http://127.0.0.1:8000"
    assert settings.oidc_audience == settings.public_url
    assert settings.database_url == f"sqlite+aiosqlite:///{tmp_path.resolve() / 'coverset.db'}"
    assert settings.admin_token is None
    assert settings.ai == "fake"
    assert settings.executor == "local"
    assert settings.untrusted_pr_policy == "approve"
    assert settings.sandbox_network == "blocked"
    assert settings.github_app_private_key_path == Path("secrets/github-app.pem")
    assert 1 <= settings.local_max_workers <= 8
    assert settings.max_bundle_bytes == 200 * 1024 * 1024
    assert settings.max_stdout_bytes == 50 * 1024 * 1024
    assert (settings.run_timeout_s, settings.pool_ttl_s, settings.pool_size) == (1800, 600, 4)
    assert (settings.max_shards, settings.max_sandboxes, settings.max_concurrent_runs) == (32, 48, 4)
    assert settings.retry_budget_per_run == 5
    assert settings.cancel_superseded is True
    assert settings.modal_daily_budget_usd == 5.0
    assert settings.ai_max_bundle_chars == 120_000
    assert (settings.shard_cpu_limit, settings.shard_memory_mib) == (2.0, 2048)
    assert settings.public_verdict_rpm == 60
    assert (
        settings.retention_runs_days,
        settings.retention_deliveries_days,
        settings.retention_events_days,
    ) == (30, 14, 7)
    assert settings.allowed_owners == [] and settings.allowed_repos == []
    assert not (settings.paused or settings.run_drafts or settings.requeue_on_boot)
    assert settings.mirrors_dir == tmp_path.resolve() / "mirrors"
    assert settings.facts_dir == tmp_path.resolve() / "facts"
    assert settings.runs_dir == tmp_path.resolve() / "runs"


def test_reads_prefixed_env_and_unprefixed_anthropic_key(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("COVERSET_MAX_SHARDS", "7")
    clean_env.setenv("COVERSET_PAUSED", "true")
    clean_env.setenv("COVERSET_AI", "anthropic")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-000000")
    settings = Settings()
    assert settings.max_shards == 7
    assert settings.paused is True
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-test-key-000000"


def test_reads_dotenv_and_ignores_empty_values(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "COVERSET_POOL_SIZE=9\nCOVERSET_ADMIN_TOKEN=\nCOVERSET_LOCAL_MAX_WORKERS=\nCOVERSET_FAKE_NOW=\n",
        encoding="utf-8",
    )
    settings = Settings()
    assert settings.pool_size == 9
    assert settings.admin_token is None
    assert settings.fake_now is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme, widgets ,,", ["acme", "widgets"]),
        ("solo", ["solo"]),
        ('["a", "b"]', ["a", "b"]),
        ("", []),
    ],
)
def test_comma_lists_from_env(clean_env: pytest.MonkeyPatch, raw: str, expected: list[str]) -> None:
    clean_env.setenv("COVERSET_ALLOWED_OWNERS", raw)
    clean_env.setenv("COVERSET_SANDBOX_DOMAIN_ALLOWLIST", raw)
    settings = Settings()
    assert settings.allowed_owners == expected
    assert settings.sandbox_domain_allowlist == expected


def test_comma_lists_from_constructor() -> None:
    settings = Settings.for_tests(allowed_repos="o/a,o/b", ai_exclude_globs=["*.env"])
    assert settings.allowed_repos == ["o/a", "o/b"]
    assert settings.ai_exclude_globs == ["*.env"]


@pytest.mark.security
@pytest.mark.parametrize("bind", ["127.0.0.1:8000", "localhost:9000", "[::1]:8000", "127.0.0.5:1"])
def test_loopback_binds_need_no_token(bind: str) -> None:
    settings = Settings.for_tests(bind=bind)
    assert settings.is_loopback


@pytest.mark.security
@pytest.mark.parametrize("bind", ["0.0.0.0:8000", "192.168.1.10:8000", "[::]:8000", "example.com:80"])
def test_non_loopback_bind_requires_long_admin_token(bind: str) -> None:
    with pytest.raises(ValidationError, match="not a loopback address"):
        Settings.for_tests(bind=bind)
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings.for_tests(bind=bind, admin_token="x" * 31)
    settings = Settings.for_tests(bind=bind, admin_token=LONG_TOKEN)
    assert not settings.is_loopback


@pytest.mark.security
def test_short_admin_token_rejected_even_on_loopback() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings.for_tests(admin_token="short")


@pytest.mark.parametrize("bind", ["8000", "host:", ":8000", "host:99999", "host:http"])
def test_malformed_bind_rejected(bind: str) -> None:
    with pytest.raises(ValidationError, match="host:port"):
        Settings.for_tests(bind=bind)


def test_ai_anthropic_requires_key() -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings.for_tests(ai="anthropic")
    settings = Settings.for_tests(ai="anthropic", anthropic_api_key="sk-ant-test-key-000000")
    assert settings.ai == "anthropic"


@pytest.mark.parametrize("audience", ["https://github.com/acme", "https://api.github.com"])
@pytest.mark.security
def test_oidc_audience_must_not_be_github(audience: str) -> None:
    with pytest.raises(ValidationError, match="github.com"):
        Settings.for_tests(oidc_audience=audience)


@pytest.mark.parametrize("public_url", ["ci.example.org", "ftp://ci.example.org", "https://", "/relative"])
def test_public_url_must_be_absolute_http(public_url: str) -> None:
    with pytest.raises(ValidationError, match="COVERSET_PUBLIC_URL"):
        Settings.for_tests(public_url=public_url)


@pytest.mark.security
def test_app_mode_requires_allowed_owners() -> None:
    with pytest.raises(ValidationError, match="COVERSET_ALLOWED_OWNERS"):
        Settings.for_tests(github_app_id=1, github_webhook_secret="w" * 20)
    settings = Settings.for_tests(github_app_id=1, github_webhook_secret="w" * 20, allowed_owners="acme")
    assert settings.allowed_owners == ["acme"]
    assert Settings.for_tests(github_token="ghp_" + "a" * 36).allowed_owners == []


@pytest.mark.security
def test_short_webhook_secret_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 16 characters"):
        Settings.for_tests(github_webhook_secret="x" * 15)
    assert Settings.for_tests(github_webhook_secret="x" * 16).github_webhook_secret is not None
    assert Settings.for_tests(github_webhook_secret="").github_webhook_secret is not None


@pytest.mark.security
def test_short_github_token_rejected() -> None:
    with pytest.raises(ValidationError, match="COVERSET_GITHUB_TOKEN must be at least 16 characters"):
        Settings.for_tests(github_token="abc")
    assert Settings.for_tests(github_token="g" * 16).github_token is not None


def test_public_url_and_audience_derivation() -> None:
    settings = Settings.for_tests(public_url="https://ci.example.org/", admin_token=LONG_TOKEN)
    assert settings.public_url == "https://ci.example.org"
    assert settings.oidc_audience == "https://ci.example.org"
    assert Settings.for_tests(bind="[::1]:9000").public_url == "http://[::1]:9000"


@pytest.mark.security
@pytest.mark.parametrize(
    "public_url", ["https://coverset-demo.ngrok.app", "https://ci.example.org", "http://192.168.1.5:8000"]
)
def test_public_url_reachable_from_outside_requires_admin_token(public_url: str) -> None:
    """A tunnel forwards internet callers to the loopback bind, where open mode trusts everyone."""
    with pytest.raises(ValidationError, match="set COVERSET_ADMIN_TOKEN"):
        Settings.for_tests(public_url=public_url)
    assert Settings.for_tests(public_url=public_url, admin_token=LONG_TOKEN).public_url == public_url


@pytest.mark.security
@pytest.mark.parametrize(
    "public_url",
    ["http://localhost:8000", "http://127.0.0.2:8000", "http://[::1]:8000", "http://ci.example.test"],
)
def test_loopback_and_special_use_public_urls_allowed_without_token(public_url: str) -> None:
    assert Settings.for_tests(public_url=public_url).admin_token is None


def test_fake_now(clean_env: pytest.MonkeyPatch) -> None:
    pinned = Settings.for_tests(fake_now=datetime(2026, 10, 10, 12, 0))
    assert pinned.now() == datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert pinned.now().tzinfo is UTC

    clean_env.setenv("COVERSET_FAKE_NOW", "2026-10-10T12:30:00Z")
    assert Settings().now() == datetime(2026, 10, 10, 12, 30, tzinfo=UTC)

    live = Settings.for_tests().now()
    assert live.tzinfo is not None
    assert abs((datetime.now(UTC) - live).total_seconds()) < 5


def test_for_tests_is_hermetic(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("COVERSET_MAX_SHARDS", "3")
    settings = Settings.for_tests()
    assert settings.max_shards == 32
    assert settings.data_dir.is_dir()


@pytest.mark.security
def test_secrets_hidden_from_repr_but_listed_for_redaction() -> None:
    settings = Settings.for_tests(admin_token=LONG_TOKEN, github_token="ghp_" + "a" * 36)
    assert LONG_TOKEN not in repr(settings)
    assert set(settings.secret_values()) == {LONG_TOKEN, "ghp_" + "a" * 36}


def test_get_settings_is_cached(clean_env: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
