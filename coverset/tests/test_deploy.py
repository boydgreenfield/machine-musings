"""Deployment: proxy headers, the secret-key override, HSTS, ``coverset deploy check`` and (opt-in)
the Docker image."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr
from quart import Quart, jsonify, request

from coverset.app import HSTS_HEADER, SECRET_KEY_FILE, create_app
from coverset.commands import deploy_cmd
from coverset.commands.deploy_cmd import Check, deployment_checks
from coverset.config import Settings, get_settings
from coverset.db.session import init_db
from coverset.db.testing import PostgresTestDatabases
from coverset.web.proxy import ProxyHeadersMiddleware, last_forwarded_value

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_URL = "https://coverset.example.com"


# -- proxy middleware ----------------------------------------------------------------------------


def _scope(headers: list[tuple[bytes, bytes]], *, kind: str = "http") -> dict[str, Any]:
    return {
        "type": kind,
        "scheme": "ws" if kind == "websocket" else "http",
        "client": ("10.0.0.1", 5555),
        "headers": headers,
        "path": "/",
    }


def test_last_forwarded_value_is_the_nearest_hop() -> None:
    headers = [(b"X-Forwarded-For", b"6.6.6.6, 1.2.3.4"), (b"x-forwarded-for", b" 5.6.7.8 ")]
    assert last_forwarded_value(headers, b"x-forwarded-for") == "5.6.7.8"
    assert last_forwarded_value([], b"x-forwarded-for") is None
    assert last_forwarded_value([(b"x-forwarded-for", b" , ")], b"x-forwarded-for") is None


@pytest.mark.security
def test_proxy_rewrite_trusts_only_the_immediate_upstream() -> None:
    """A caller-supplied first value (the spoof) is ignored; the proxy's appended value wins."""
    scope = _scope([(b"x-forwarded-for", b"127.0.0.1, 203.0.113.9"), (b"x-forwarded-proto", b"https")])
    rewritten = ProxyHeadersMiddleware.rewrite(scope)
    assert rewritten["client"] == ("203.0.113.9", 0)
    assert rewritten["scheme"] == "https"
    assert scope["client"] == ("10.0.0.1", 5555), "the original scope is not mutated"


@pytest.mark.parametrize(
    ("forwarded_for", "client"),
    [
        (b"[2001:db8::1]:4711", "2001:db8::1"),
        (b"198.51.100.7:1234", "198.51.100.7"),
        (b"2001:db8::2", "2001:db8::2"),
        (b"unknown", "10.0.0.1"),
        (b"_hidden, not-an-ip", "10.0.0.1"),
    ],
)
def test_proxy_rewrite_accepts_only_ip_addresses(forwarded_for: bytes, client: str) -> None:
    rewritten = ProxyHeadersMiddleware.rewrite(_scope([(b"x-forwarded-for", forwarded_for)]))
    assert rewritten["client"][0] == client


def test_proxy_rewrite_ignores_unknown_schemes_and_maps_websockets() -> None:
    assert ProxyHeadersMiddleware.rewrite(_scope([(b"x-forwarded-proto", b"gopher")]))["scheme"] == "http"
    ws = ProxyHeadersMiddleware.rewrite(_scope([(b"x-forwarded-proto", b"HTTPS")], kind="websocket"))
    assert ws["scheme"] == "wss"


def test_proxy_rewrite_leaves_host_alone() -> None:
    """X-Forwarded-Host is not honoured: links come from COVERSET_PUBLIC_URL, Host checks see Host."""
    headers = [(b"host", b"coverset.internal"), (b"x-forwarded-host", b"evil.example")]
    assert ProxyHeadersMiddleware.rewrite(_scope(headers))["headers"] == headers


@pytest.mark.security
async def test_proxy_middleware_uses_fly_client_ip_on_fly() -> None:
    """Fly appends its own ingress IP to X-Forwarded-For; Fly-Client-IP is the caller there."""
    seen: list[Any] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["client"])

    headers = [(b"x-forwarded-for", b"198.51.100.4, 66.241.124.1"), (b"fly-client-ip", b"198.51.100.4")]
    on_fly = ProxyHeadersMiddleware.for_environment(inner, {"FLY_APP_NAME": "coverset"})  # type: ignore[arg-type]
    elsewhere = ProxyHeadersMiddleware.for_environment(inner, {})  # type: ignore[arg-type]
    for middleware in (on_fly, elsewhere):
        await middleware(_scope(headers), None, None)  # type: ignore[arg-type]
    assert seen == [("198.51.100.4", 0), ("66.241.124.1", 0)]


async def test_proxy_middleware_passes_lifespan_through() -> None:
    seen: list[dict[str, Any]] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope)

    async def receive() -> dict[str, Any]:
        return {}

    async def send(message: Any) -> None:
        return None

    lifespan = {"type": "lifespan", "asgi": {"version": "3.0"}}
    await ProxyHeadersMiddleware(inner)(lifespan, receive, send)  # type: ignore[arg-type]
    assert seen == [lifespan]


def _probe_app(settings: Settings) -> Quart:
    """The real app plus one route that reports what the request looks like inside Quart."""
    app = create_app(settings)

    @app.get("/v1/_probe")
    async def _probe() -> Any:
        return jsonify(
            remote=request.remote_addr,
            scheme=request.scheme,
            external=request.url,
            forwarded_header=request.headers.get("X-Forwarded-For"),
        )

    return app


@pytest.mark.security
@pytest.mark.parametrize("trust", [True, False])
async def test_trust_proxy_sets_remote_addr_and_scheme(
    settings: Settings, admin_token: str, trust: bool
) -> None:
    app = _probe_app(
        settings.model_copy(update={"trust_proxy": trust, "admin_token": SecretStr(admin_token)})
    )
    async with app.test_app() as test_app:
        response = await test_app.test_client().get(
            "/v1/_probe",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Forwarded-For": "6.6.6.6, 198.51.100.20",
                "X-Forwarded-Proto": "https",
            },
        )
        body = await response.get_json()
    if trust:
        assert body["remote"] == "198.51.100.20"
        assert body["scheme"] == "https"
        assert body["external"].startswith("https://")
    else:
        assert body["remote"] not in ("198.51.100.20", "6.6.6.6")
        assert body["scheme"] == "http"
    # The header stays visible, so /setup still knows a proxy is involved.
    assert body["forwarded_header"] == "6.6.6.6, 198.51.100.20"


# -- secret key and HSTS -------------------------------------------------------------------------


@pytest.mark.security
def test_secret_key_override_mixes_in_the_admin_token(settings: Settings, admin_token: str) -> None:
    def key_for(**update: Any) -> str:
        return create_app(settings.model_copy(update=update)).config["SECRET_KEY"]

    base = {"secret_key": SecretStr("k" * 64), "admin_token": SecretStr(admin_token)}
    first = key_for(**base)
    assert first == key_for(**base), "replicas with the same key and token agree"
    assert "k" * 64 not in first and admin_token not in first
    assert first != key_for(admin_token=SecretStr(admin_token)), "differs from the token-only key"
    assert first != key_for(**{**base, "admin_token": SecretStr(admin_token + "x")}), (
        "token rotation signs out"
    )
    assert first != key_for(**{**base, "secret_key": SecretStr("j" * 64)}), "key rotation signs out"
    assert not (settings.data_dir / SECRET_KEY_FILE).exists()


@pytest.mark.security
def test_empty_secret_key_falls_back_to_the_key_file(settings: Settings) -> None:
    key = create_app(settings.model_copy(update={"secret_key": SecretStr("")})).config["SECRET_KEY"]
    assert (settings.data_dir / SECRET_KEY_FILE).read_text(encoding="utf-8") == key


@pytest.mark.security
async def test_hsts_only_on_https_public_urls(tmp_path: Path, admin_token: str, settings: Settings) -> None:
    db = settings.database_url  # the test's own database, for both apps in turn
    https = Settings.for_tests(
        data_dir=tmp_path / "a", public_url=PUBLIC_URL, admin_token=SecretStr(admin_token), database_url=db
    )
    plain = Settings.for_tests(data_dir=tmp_path / "b", database_url=db)
    for settings, expected in ((https, HSTS_HEADER), (plain, None)):
        app = create_app(settings)
        async with app.test_app() as test_app:
            response = await test_app.test_client().get("/healthz")
        assert response.headers.get("Strict-Transport-Security") == expected


# -- coverset deploy check -----------------------------------------------------------------------


def _rows(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


@pytest.fixture
def no_modal_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide this machine's ~/.modal.toml from the checks."""
    monkeypatch.setattr(deploy_cmd, "_profile_has_token", lambda: False)


def test_deploy_check_on_a_ready_server(
    tmp_path: Path, admin_token: str, no_modal_profile: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_CACHE_MAX_GB", "0.01")  # a bound this machine's free disk holds
    settings = Settings.for_tests(
        env="prod",
        data_dir=tmp_path / "data",
        bind="0.0.0.0:8000",
        public_url=PUBLIC_URL,
        admin_token=SecretStr(admin_token),
        secret_key=SecretStr("s" * 64),
        trust_proxy=True,
        executor="modal",
        github_app_id=123,
        github_app_client_id="Iv23liExample",
        github_app_client_secret=SecretStr("c" * 40),
        github_app_private_key=SecretStr(
            "-----BEGIN PRIVATE KEY-----\nMIIexample\n-----END PRIVATE KEY-----\n"
        ),
        github_webhook_secret=SecretStr("w" * 32),
        allowed_owners=["octo-org"],
        database_url="postgresql+asyncpg://coverset:pw@db.internal/coverset",
        blob_url="s3://coverset-prod/cs?region=us-east-1",
    )
    probed: list[str] = []

    def prober(url: str) -> tuple[bool, str]:
        probed.append(url)
        return True, "HTTP 200, coverset 0.1.0"

    rows = _rows(
        deployment_checks(
            settings,
            environ={"MODAL_TOKEN_ID": "ak-x", "MODAL_TOKEN_SECRET": "as-y"},
            prober=prober,
            in_modal=False,
            check_database=False,
            store_probe=lambda _settings: ("pass", "s3://coverset-prod/cs: conditional writes ok"),
        )
    )
    assert probed == [f"{PUBLIC_URL}/healthz"]
    assert rows["state"].detail.startswith("hosted")
    assert {name: row.status for name, row in rows.items() if row.status != "pass"} == {}
    assert "MODAL_TOKEN_ID" in rows["executor"].detail
    assert "octo-org" in rows["github"].detail
    output = " ".join(row.detail for row in rows.values())
    assert admin_token not in output and "s" * 64 not in output and "w" * 32 not in output


def test_deploy_check_flags_what_is_missing(tmp_path: Path, admin_token: str, no_modal_profile: None) -> None:
    settings = Settings.for_tests(
        env="test",
        data_dir=tmp_path / "data",
        bind="0.0.0.0:8000",
        public_url="http://coverset.example.com",
        admin_token=SecretStr(admin_token),
        executor="modal",
        github_app_id=7,
        allowed_owners=["me"],
    )
    rows = _rows(
        deployment_checks(
            settings,
            environ={},
            prober=lambda _url: (False, "unreachable: timed out"),
            in_modal=False,
            check_database=False,
        )
    )
    assert rows["environment"].status == "warn"
    assert rows["public url"].status == "warn" and "not https" in rows["public url"].detail
    assert rows["reachable"].status == "warn"
    assert rows["secret key"].status == "pass" and "derived" in rows["secret key"].detail
    assert rows["executor"].status == "fail"
    assert rows["github"].status == "fail" and "COVERSET_GITHUB_APP_PRIVATE_KEY" in rows["github"].detail
    assert rows["github login"].status == "warn"
    assert rows["webhook secret"].status == "fail"


def test_deploy_check_inside_modal(tmp_path: Path, admin_token: str, no_modal_profile: None) -> None:
    settings = Settings.for_tests(
        env="prod",
        data_dir=tmp_path / "data",
        public_url=PUBLIC_URL,
        admin_token=SecretStr(admin_token),
        executor="modal",
    )
    rows = _rows(deployment_checks(settings, environ={}, prober=None, in_modal=True, check_database=False))
    assert "reachable" not in rows
    assert rows["bind"].status == "pass" and "Modal" in rows["bind"].detail
    assert rows["proxy"].status == "pass" and "Modal" in rows["proxy"].detail
    assert rows["executor"].status == "pass" and "container runtime" in rows["executor"].detail
    assert rows["github"].status == "warn" and rows["webhook secret"].status == "warn"


def test_modal_container_detected_from_the_environment(tmp_path: Path, admin_token: str) -> None:
    """A ``modal container exec`` shell has MODAL_IS_REMOTE=1 although ``modal.is_local()`` is True."""
    settings = Settings.for_tests(
        data_dir=tmp_path / "data",
        public_url=PUBLIC_URL,
        admin_token=SecretStr(admin_token),
        executor="modal",
    )
    rows = _rows(
        deployment_checks(settings, environ={"MODAL_IS_REMOTE": "1"}, prober=None, check_database=False)
    )
    assert "container runtime" in rows["executor"].detail


def test_deploy_check_database_rows(tmp_path: Path, postgres_databases: PostgresTestDatabases) -> None:
    """Sync on purpose: the check runs its own event loop, as the CLI does. A database the server
    does not have yet fails (the check never creates one) and names the command that does."""
    url = postgres_databases.url_for(f"{postgres_databases.prefix}_deploy_check")
    settings = Settings.for_tests(data_dir=tmp_path / "data", database_url=url)
    try:
        missing = _rows(deployment_checks(settings, environ={}, prober=None, in_modal=False))["database"]
        assert missing.status == "fail" and "does not exist" in missing.detail
        assert "coverset init-db" in missing.detail
        asyncio.run(init_db(settings))
        migrated = _rows(deployment_checks(settings, environ={}, prober=None, in_modal=False))["database"]
        assert migrated.status == "pass", migrated.detail
        assert "lease coverset:background: free" in migrated.detail, "the lease holder, as db status had it"
    finally:
        postgres_databases.drop(url)


def test_deploy_check_reads_the_app_setup_stored(tmp_path: Path, settings: Settings) -> None:
    """Without GitHub credentials in the environment the github rows describe the App ``/setup``
    sealed into the database; without COVERSET_SECRET_KEY they say /setup cannot store one."""
    from coverset.db.session import Database
    from coverset.forge.credentials import AppCredentials, store_app

    keyed = settings.model_copy(update={"secret_key": SecretStr("k" * 40)})
    assert (
        "COVERSET_SECRET_KEY"
        in _rows(deployment_checks(settings, environ={}, prober=None, in_modal=False))["github"].detail
    )

    async def store() -> None:
        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:
                app = AppCredentials(
                    app_id=4242,
                    pem="-----BEGIN RSA PRIVATE KEY-----\nMII\n-----END RSA PRIVATE KEY-----\n",
                    client_id="Iv1.x",
                    client_secret="c" * 40,
                    webhook_secret="w" * 32,
                    owner="octo-org",
                )
                await store_app(session, keyed, app)
                await session.commit()
        finally:
            await database.engine.dispose()

    asyncio.run(store())
    rows = _rows(deployment_checks(keyed, environ={}, prober=None, in_modal=False))
    assert rows["github"].status == "pass" and "stored by /setup" in rows["github"].detail
    assert "octo-org" in rows["github"].detail and rows["webhook secret"].status == "pass"
    assert "github login" not in rows
    other_key = keyed.model_copy(update={"secret_key": SecretStr("z" * 40)})
    rows = _rows(deployment_checks(other_key, environ={}, prober=None, in_modal=False))
    assert rows["github"].status == "fail" and "cannot be used" in rows["github"].detail
    output = " ".join(row.detail for row in rows.values())
    assert "c" * 40 not in output and "w" * 32 not in output


def test_deploy_check_reports_an_unwritable_data_dir(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    settings = Settings.for_tests(data_dir=blocker / "data")
    rows = _rows(deployment_checks(settings, environ={}, prober=None, in_modal=False, check_database=False))
    assert rows["data dir"].status == "fail"


def test_deploy_check_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_modal_profile: None, settings: Settings
) -> None:
    from coverset.cli import cli

    monkeypatch.chdir(tmp_path)  # no stray .env
    for key in [key for key in os.environ if key.startswith("COVERSET_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("COVERSET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COVERSET_DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["deploy", "check", "--offline"])
        assert result.exit_code == 0, result.output
        assert "PASS  data dir" in result.output
        assert "WARN  admin token" in result.output
        assert "reachable" not in result.output
        json_result = CliRunner().invoke(cli, ["deploy", "check", "--offline", "--json"])
        assert '"name": "data dir"' in json_result.output

        monkeypatch.setenv("COVERSET_EXECUTOR", "modal")
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        get_settings.cache_clear()
        failing = CliRunner().invoke(cli, ["deploy", "check", "--offline"])
        assert failing.exit_code == 1
        assert "FAIL  executor" in failing.output

        monkeypatch.setenv("COVERSET_BIND", "0.0.0.0:8000")  # invalid without a token
        get_settings.cache_clear()
        invalid = CliRunner().invoke(cli, ["deploy", "check", "--offline"])
        assert invalid.exit_code == 1 and "FAIL  configuration" in invalid.output
    finally:
        get_settings.cache_clear()


def test_probe_healthz_accepts_only_coverset(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.request

    class Reply(io.BytesIO):
        status = 200

        def __enter__(self) -> Reply:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *_a, **_k: Reply(b'{"status": "ok", "version": "9"}')
    )
    assert deploy_cmd.probe_healthz("https://x.example/healthz") == (True, "HTTP 200, coverset 9")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: Reply(b"<html>parked</html>"))
    assert deploy_cmd.probe_healthz("https://x.example/healthz")[0] is False


# -- the Docker image (opt-in) -------------------------------------------------------------------


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False).returncode == 0
        )
    except subprocess.TimeoutExpired:
        return False


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("COVERSET_DOCKER_TESTS") != "1",
    reason="builds the image (minutes, network): set COVERSET_DOCKER_TESTS=1",
)
def test_docker_image_builds_and_imports() -> None:
    """``docker build`` of the project's Dockerfile, then the app imports and the CLI runs in it.

    Opt-in rather than merely "skipped without docker" because a full-suite run should not build
    images, and on some desktops ``docker`` blocks on a credential-helper prompt.
    """
    if not _docker_available():
        pytest.skip("docker is not available")
    tag = "coverset:test-deploy"
    build = subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(PROJECT_ROOT)], capture_output=True, text=True, timeout=1500
    )
    assert build.returncode == 0, build.stderr[-2000:]
    # OPENSSL_armcap=0: OpenSSL's ARM feature probe hits SIGILL in Docker Desktop's VM on Apple M4.
    run = subprocess.run(
        ["docker", "run", "--rm", "-e", "OPENSSL_armcap=0", "--entrypoint", "coverset", tag, "--version"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert "coverset" in run.stdout
