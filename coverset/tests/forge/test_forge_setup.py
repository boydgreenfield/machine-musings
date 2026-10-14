"""``/setup``: the manifest form, state checks, the mocked code exchange, the App sealed into the
database, overwrite refusal, environment credentials, "Forget the App", tunnel refusal, the
code-free finish step, and every replica picking the stored App up without a restart."""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from quart import Quart
from quart.typing import TestClientProtocol as Client
from sqlalchemy import select

from coverset.app import create_app
from coverset.config import Settings
from coverset.db.models import AppSetting, AuditEvent
from coverset.db.session import Database
from coverset.db.vault import Vault
from coverset.forge.credentials import APP_ROW, AppCredentials, CredentialCache, store_app
from coverset.forge.fake import FakeForge
from coverset.forge.github import API_VERSION, GitHubForge
from coverset.forge.manifest import build_manifest
from coverset.forge.registry import CREDENTIALS_EXTENSION, LiveForge, forge_for_app
from coverset.forge.signing import sign_body
from coverset.logging import RedactingFilter, redact
from coverset.web.setup import TRANSPORT_EXTENSION

SECRET_KEY = "setup-test-secret-key-0123456789abcdef0123"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIfakeKeyMaterialForTests0123456789\n-----END RSA PRIVATE KEY-----\n"
CONVERSION = {
    "id": 4242,
    "slug": "coverset-test",
    "owner": {"login": "coverset-demo", "id": 777},
    "html_url": "https://github.com/apps/coverset-test",
    "client_id": "Iv1.0123456789abcdef",
    "client_secret": "client-secret-0123456789abcdef0123456789",
    "webhook_secret": "webhook-secret-0123456789abcdef",
    "pem": PEM,
}
SECRETS = (CONVERSION["client_secret"], CONVERSION["webhook_secret"], "MIIfakeKeyMaterial", SECRET_KEY)
PING = json.dumps({"zen": "Keep it logically awesome.", "hook_id": 480001}).encode()


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    """No GitHub credentials in the environment, and a secret key for the vault."""
    return {"secret_key": SecretStr(SECRET_KEY)}


def mock_github(app: Quart, conversion: dict[str, Any] | None = None) -> list[httpx.Request]:
    """Mock GitHub's manifest conversion endpoint for ``app``; returns the requests it saw."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path == "/app-manifests/good-code/conversions":
            return httpx.Response(201, json=conversion or CONVERSION)
        return httpx.Response(404, json={"message": "Not Found"})

    app.extensions[TRANSPORT_EXTENSION] = httpx.MockTransport(handler)
    return seen


@pytest.fixture
def exchange(app: Quart) -> list[httpx.Request]:
    return mock_github(app)


LOCAL = {"client": ("127.0.0.1", 50123)}
"""ASGI scope of a browser on the machine running coverset (the test client has no address)."""


async def start(client: Client) -> tuple[str, str]:
    """Load /setup; return (page html, state)."""
    response = await client.get("/setup", scope_base=LOCAL)
    assert response.status_code == 200
    page = await response.get_data(as_text=True)
    match = re.search(r'action="https://github.com/settings/apps/new\?state=([^"]+)"', page)
    assert match is not None
    return page, html.unescape(match.group(1))


async def callback(client: Client, query: str, **kwargs: Any) -> Any:
    kwargs.setdefault("scope_base", LOCAL)
    return await client.get(f"/setup/github/callback?{query}", **kwargs)


async def finish_setup(client: Client) -> Any:
    """The whole browser round trip as the local operator; returns the finish step's response."""
    _, state = await start(client)
    arrival = await callback(client, f"code=good-code&state={state}")
    assert arrival.status_code == 303
    return await client.get(arrival.headers["Location"], scope_base=LOCAL)


async def stored_rows(app: Quart) -> dict[str, str]:
    async with app.extensions["db"].session() as session:
        return dict((await session.execute(select(AppSetting.key, AppSetting.value))).all())


async def audit_actions(app: Quart) -> list[str]:
    async with app.extensions["db"].session() as session:
        return list(await session.scalars(select(AuditEvent.action).order_by(AuditEvent.id)))


async def ping(client: Client, secret: str | None) -> Any:
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": "22a1a4f3-a9e5-5dc9-a996-29b57b67bf74",
    }
    if secret is not None:
        headers["X-Hub-Signature-256"] = sign_body(secret, PING)
    return await client.post("/webhooks/github", data=PING, headers=headers)


async def test_setup_page_renders_the_manifest_form(
    client: Client, app: Quart, exchange: list[httpx.Request]
) -> None:
    page, state = await start(client)
    assert len(state) >= 40
    manifest_value = re.search(r'name="manifest" value="([^"]+)"', page)
    assert manifest_value is not None
    manifest = json.loads(html.unescape(manifest_value.group(1)))
    public_url = app.extensions["settings"].public_url
    assert manifest["public"] is False
    assert manifest["hook_attributes"]["url"] == f"{public_url}/webhooks/github"
    assert manifest["redirect_url"] == f"{public_url}/setup/github/callback"
    assert manifest["default_permissions"] == {
        "checks": "write",
        "contents": "read",
        "pull_requests": "write",
        "issues": "write",
        "metadata": "read",
    }
    assert set(manifest["default_events"]) == {
        "pull_request",
        "push",
        "check_run",
        "check_suite",
        "repository",
    }
    assert "<script" not in page and "loopback address" in page and "COVERSET_ADMIN_TOKEN" in page
    assert "COVERSET_GITHUB_TOKEN" in page and "Allowed owners" in page
    assert "encrypted under <code>COVERSET_SECRET_KEY</code>" in page and "Restart" not in page
    _, second_state = await start(client)
    assert second_state != state


async def test_org_form_and_bad_org(client: Client, exchange: list[httpx.Request]) -> None:
    response = await client.get("/setup?org=coverset-demo")
    assert (
        "https://github.com/organizations/coverset-demo/settings/apps/new?state="
        in await response.get_data(as_text=True)
    )
    assert (await client.get("/setup?org=../evil")).status_code == 400


@pytest.mark.security
async def test_the_exchange_seals_the_app_into_the_database(
    client: Client, app: Quart, exchange: list[httpx.Request], tmp_path: Path, settings: Settings
) -> None:
    _, state = await start(client)
    arrival = await callback(client, f"code=good-code&state={state}")
    assert arrival.status_code == 303, "the code moves into the session first"
    location = arrival.headers["Location"]
    assert location.endswith("/setup/github/finish") and "good-code" not in location
    assert arrival.headers["Cache-Control"] == "no-store" and exchange == []
    response = await client.get(location, scope_base=LOCAL)
    page = await response.get_data(as_text=True)
    assert response.status_code == 200, page
    assert "GitHub App connected" in page and "Credentials stored" in page
    assert "https://github.com/apps/coverset-test/installations/new" in page and "coverset-test" in page
    assert "nothing needs a restart" in page and "Restart" not in page
    assert CONVERSION["client_secret"] not in page and "BEGIN RSA" not in page
    (request,) = exchange
    assert request.headers["X-GitHub-Api-Version"] == API_VERSION

    rows = await stored_rows(app)
    assert set(rows) == {APP_ROW}
    for secret in (*SECRETS, "4242", "coverset-test"):
        assert secret not in rows[APP_ROW], "the whole App is sealed, ids included"
    stored = AppCredentials.from_json(Vault(SECRET_KEY).open(APP_ROW, rows[APP_ROW]))
    assert (stored.app_id, stored.slug, stored.owner, stored.pem) == (
        4242,
        "coverset-test",
        "coverset-demo",
        PEM,
    )
    assert stored.client_secret == CONVERSION["client_secret"]
    assert stored.webhook_secret == CONVERSION["webhook_secret"] and stored.created_at
    assert not (tmp_path / ".env").exists() and not list(settings.data_dir.rglob("*.pem"))
    assert await audit_actions(app) == ["github_app_stored"]

    replay = await callback(client, f"code=good-code&state={state}")
    assert replay.status_code == 400, "the state is single use"
    again = await client.get("/setup/github/finish", scope_base=LOCAL)
    assert again.status_code == 400 and len(exchange) == 1, "the code is single use"
    page = await (await client.get("/setup", scope_base=LOCAL)).get_data(as_text=True)
    assert "the App <code>coverset-test</code>" in page and "Forget the App" in page
    assert "<code>coverset-demo</code> (the stored App's owner)" in page


@pytest.mark.security
async def test_bad_or_missing_state_is_400(client: Client, exchange: list[httpx.Request]) -> None:
    await start(client)
    for query in ("code=good-code&state=wrong", "code=good-code", "state="):
        response = await callback(client, query)
        assert response.status_code == 400, query
    assert (await client.get("/setup/github/finish", scope_base=LOCAL)).status_code == 400
    assert exchange == []


@pytest.mark.security
async def test_a_second_setup_without_an_admin_overwrite_is_refused(
    client: Client, app: Quart, exchange: list[httpx.Request]
) -> None:
    """``overwrite`` always needs an admin session; a query parameter never grants it."""
    assert (await finish_setup(client)).status_code == 200
    first = await stored_rows(app)
    _, state = await start(client)
    arrival = await callback(client, f"code=good-code&state={state}&overwrite=1")
    refused = await client.get(arrival.headers["Location"], scope_base=LOCAL)
    page = await refused.get_data(as_text=True)
    assert refused.status_code == 409 and "Credentials already stored" in page and "coverset-test" in page
    assert "good-code" not in page and 'name="overwrite"' not in page and "COVERSET_ADMIN_TOKEN" in page
    async with client.session_transaction() as session:
        session["csrf"] = "known-token"
    forced = await client.post(
        "/setup/github/finish", form={"overwrite": "1", "csrf_token": "known-token"}, scope_base=LOCAL
    )
    assert forced.status_code == 403
    assert len(exchange) == 1 and await stored_rows(app) == first


async def test_concurrent_first_setups_store_one_app(settings: Settings, app: Quart) -> None:
    """The insert itself refuses a second App, so two finish steps racing cannot both store one."""
    from coverset.forge.credentials import CredentialsExist

    one = AppCredentials(app_id=1, pem=PEM, slug="one", owner="coverset-demo")
    two = AppCredentials(app_id=2, pem=PEM, slug="two", owner="coverset-demo")
    database: Database = app.extensions["db"]
    async with database.session() as first, database.session() as second:
        await store_app(first, settings, one)
        await first.commit()
        with pytest.raises(CredentialsExist):
            await store_app(second, settings, two)
    rows = await stored_rows(app)
    assert AppCredentials.from_json(Vault(SECRET_KEY).open(APP_ROW, rows[APP_ROW])).slug == "one"


@pytest.mark.security
async def test_environment_credentials_win_and_make_setup_read_only(
    tmp_path: Path, settings: Settings
) -> None:
    environment = settings.model_copy(
        update={
            "github_app_id": 99,
            "github_app_private_key": SecretStr(PEM),
            "github_webhook_secret": SecretStr("env-webhook-secret-0123456789"),
            "allowed_owners": ["acme"],
        }
    )
    async with database_with_app(settings):  # a stored App the environment must override
        pass
    app = create_app(environment)
    seen = mock_github(app)
    async with app.test_app():
        client = app.test_client()
        page, _ = await start(client)
        assert "from the environment" in page and "<code>COVERSET_GITHUB_APP_ID</code>" in page
        assert "this page cannot change them" in page and "disabled" in page
        response = await finish_setup(client)
        body = await response.get_data(as_text=True)
        assert response.status_code == 409 and "GitHub credentials come from the environment" in body
        assert seen == [], "nothing was exchanged"
        forge = await forge_for_app(app).current()  # type: ignore[attr-defined]
        assert isinstance(forge, GitHubForge) and forge.app_id == 99, (
            "the environment's App, not the stored one"
        )
        assert (await ping(client, "env-webhook-secret-0123456789")).status_code == 202
        assert (await ping(client, CONVERSION["webhook_secret"])).status_code == 403
        async with client.session_transaction() as session:
            session["csrf"] = "known-token"
        forget = await client.post(
            "/setup/github/forget", form={"csrf_token": "known-token"}, scope_base=LOCAL
        )
        assert forget.status_code == 409
        assert set(await stored_rows(app)) == {APP_ROW}, "nothing forgotten either"


class database_with_app:  # noqa: N801 - used as a context manager
    """Store :data:`CONVERSION`'s App in ``settings``' database (as another replica's /setup would)."""

    def __init__(self, settings: Settings, **changes: Any) -> None:
        self.settings = settings
        values: dict[str, Any] = {
            "app_id": int(CONVERSION["id"]),
            "pem": PEM,
            "client_id": str(CONVERSION["client_id"]),
            "client_secret": str(CONVERSION["client_secret"]),
            "webhook_secret": str(CONVERSION["webhook_secret"]),
            "slug": str(CONVERSION["slug"]),
            "owner": "coverset-demo",
            "html_url": str(CONVERSION["html_url"]),
        }
        self.app = AppCredentials(**{**values, **changes})

    async def __aenter__(self) -> AppCredentials:
        database = Database.from_url(self.settings.database_url)
        try:
            async with database.session() as session:
                stored = await store_app(session, self.settings, self.app, overwrite=True)
                await session.commit()
        finally:
            await database.engine.dispose()
        return stored

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_no_secret_key_refuses_before_the_exchange(tmp_path: Path, settings: Settings) -> None:
    bare = settings.model_copy(update={"secret_key": None})
    app = create_app(bare)
    seen = mock_github(app)
    async with app.test_app():
        client = app.test_client()
        page, _ = await start(client)
        assert "COVERSET_SECRET_KEY</code> is not set" in page and "disabled" in page
        response = await finish_setup(client)
        body = await response.get_data(as_text=True)
        assert response.status_code == 503 and "COVERSET_SECRET_KEY is not set" in body
        assert "openssl rand -hex 32" in body and seen == [] and await stored_rows(app) == {}


async def test_the_forge_picks_the_stored_app_up_without_a_restart(
    settings: Settings, app: Quart, client: Client, exchange: list[httpx.Request]
) -> None:
    """Another replica (a second app on the same database) switches from the fake forge to the
    stored App on its next credentials check, at most REFRESH_S later; forgetting switches back."""
    replica = create_app(settings)
    now = [1000.0]
    replica.extensions[CREDENTIALS_EXTENSION] = CredentialCache(
        settings, replica.extensions["db"].sessionmaker, clock=lambda: now[0]
    )
    async with replica.test_app():
        other = replica.test_client()
        live = forge_for_app(replica)
        assert isinstance(live, LiveForge) and isinstance(await live.current(), FakeForge)
        assert (await ping(other, CONVERSION["webhook_secret"])).status_code == 503, "no secret yet"

        assert (await finish_setup(client)).status_code == 200  # /setup on the first replica
        here = await forge_for_app(app).current()  # type: ignore[attr-defined]
        assert isinstance(here, GitHubForge) and here.app_id == 4242, "this replica: at once"

        assert isinstance(await live.current(), FakeForge), "the other replica re-checks every few seconds"
        now[0] += 5.0
        there = await live.current()
        assert isinstance(there, GitHubForge) and there.app_id == 4242 and live.name == "github"
        assert await live.current() is there, "rebuilt only when the credentials change"
        login = await (await other.get("/login")).get_data(as_text=True)
        assert "Sign in with GitHub" in login, "the stored client id and secret enable sign-in"

        async with client.session_transaction() as session:
            session["csrf"] = "known-token"
        forgotten = await client.post(
            "/setup/github/forget", form={"csrf_token": "known-token"}, scope_base=LOCAL
        )
        assert forgotten.status_code == 200 and "GitHub App forgotten" in await forgotten.get_data(
            as_text=True
        )
        now[0] += 5.0
        assert isinstance(await live.current(), FakeForge)
    assert await stored_rows(app) == {}
    assert await audit_actions(app) == ["github_app_stored", "github_app_forgotten"]


async def test_a_worker_sees_the_stored_app_before_it_reads_the_adapter(
    settings: Settings, app: Quart, client: Client, exchange: list[httpx.Request]
) -> None:
    """Outside the web app nothing refreshes a LiveForge's synchronous view (``name``,
    ``capabilities()``): a fresh orchestrator context over a database holding the App would gate
    an upload run's mirror refresh and the verdict check on the fake forge. ``current_forge``
    refreshes first, so the run paths see the stored App, and no App after a forget."""
    from coverset.db.models import Repo
    from coverset.forge.credentials import forget_app
    from coverset.orchestrator.runner import OrchestratorContext
    from coverset.orchestrator.upload import _can_fetch

    assert (await finish_setup(client)).status_code == 200
    now = [1000.0]
    ctx = OrchestratorContext.from_settings(settings, db=app.extensions["db"])
    live = ctx.get_forge()
    assert isinstance(live, LiveForge)
    live.credentials = CredentialCache(settings, app.extensions["db"].sessionmaker, clock=lambda: now[0])
    repo = Repo(forge="github", external_id="1", full_name="coverset-demo/repo")
    assert live.name == "fake" and not _can_fetch(repo, live), "the never-refreshed view"

    forge = await ctx.current_forge()
    assert isinstance(forge, GitHubForge) and forge.app_id == 4242
    assert _can_fetch(repo, forge) and forge.capabilities().check_runs, "the run paths' view"
    assert live.name == "github", "and the synchronous view followed"

    async with app.extensions["db"].session() as session:
        assert await forget_app(session)
        await session.commit()
    assert await ctx.current_forge() is forge, "within REFRESH_S the cached view stands"
    now[0] += 5.0
    assert isinstance(await ctx.current_forge(), FakeForge) and live.name == "fake"


@pytest.mark.security
async def test_the_webhook_verifies_with_the_stored_secret(
    client: Client, app: Quart, exchange: list[httpx.Request]
) -> None:
    assert (await ping(client, CONVERSION["webhook_secret"])).status_code == 503, "fail closed before setup"
    assert (await finish_setup(client)).status_code == 200
    accepted = await ping(client, CONVERSION["webhook_secret"])
    assert accepted.status_code == 202 and (await accepted.get_json())["reason"] == "ping"
    assert (await ping(client, "some-other-secret-0123456789")).status_code == 403
    assert (await ping(client, None)).status_code == 403


@pytest.mark.security
async def test_the_stored_apps_owner_is_the_default_allowed_owner(
    client: Client, app: Quart, exchange: list[httpx.Request], settings: Settings
) -> None:
    """With no COVERSET_ALLOWED_OWNERS only the App's own owner is accepted (an empty list would
    accept everyone; /setup used to write the owner into .env), and an explicit list wins."""
    from forge_helpers import load_fixture

    from coverset.forge.credentials import GitHubCredentials, with_app_owner

    assert (await finish_setup(client)).status_code == 200
    headers, body = load_fixture("installation_created")  # owner coverset-demo
    headers["X-Hub-Signature-256"] = sign_body(CONVERSION["webhook_secret"], body)
    accepted = await client.post("/webhooks/github", data=body, headers=headers)
    assert accepted.status_code == 202 and (await accepted.get_json())["status"] != "ignored_owner"
    foreign = body.replace(b"coverset-demo", b"stranger-org")
    headers["X-Hub-Signature-256"] = sign_body(CONVERSION["webhook_secret"], foreign)
    headers["X-GitHub-Delivery"] = "5f0e7e2a-2c1b-4c55-9d1a-0b8c2d7e9f10"
    refused = await client.post("/webhooks/github", data=foreign, headers=headers)
    assert refused.status_code == 202 and (await refused.get_json())["status"] == "ignored_owner"
    stored = GitHubCredentials("database", app=AppCredentials(app_id=1, pem=PEM, owner="coverset-demo"))
    assert with_app_owner(settings, stored).allowed_owners == ["coverset-demo"]
    narrowed = settings.model_copy(update={"allowed_owners": ["acme"]})
    assert with_app_owner(narrowed, stored) is narrowed
    environment = GitHubCredentials("environment", app=AppCredentials(app_id=1, pem=PEM, owner="x"))
    assert with_app_owner(settings, environment) is settings, "environment Apps need the list"


@pytest.mark.security
async def test_forget_needs_the_csrf_token_and_a_local_or_admin_caller(
    client: Client, app: Quart, exchange: list[httpx.Request]
) -> None:
    assert (await finish_setup(client)).status_code == 200
    without = await client.post("/setup/github/forget", form={}, scope_base=LOCAL)
    assert without.status_code == 403
    async with client.session_transaction() as session:
        session["csrf"] = "known-token"
    remote = await client.post(
        "/setup/github/forget",
        form={"csrf_token": "known-token"},
        scope_base=LOCAL,
        headers={"X-Forwarded-For": "203.0.113.66"},
    )
    assert remote.status_code == 403 and set(await stored_rows(app)) == {APP_ROW}
    done = await client.post("/setup/github/forget", form={"csrf_token": "known-token"}, scope_base=LOCAL)
    body = await done.get_data(as_text=True)
    assert done.status_code == 200 and "https://github.com/apps/coverset-test" in body
    assert await stored_rows(app) == {}
    again = await client.post("/setup/github/forget", form={"csrf_token": "known-token"}, scope_base=LOCAL)
    assert again.status_code == 404
    assert await audit_actions(app) == ["github_app_stored", "github_app_forgotten"]


async def test_a_row_sealed_under_another_key_is_reported_not_used(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    async with database_with_app(settings.model_copy(update={"secret_key": SecretStr("x" * 40)})):
        pass
    app = create_app(settings)
    caplog.set_level(logging.ERROR, logger="coverset.forge.credentials")
    async with app.test_app():
        client = app.test_client()
        page, _ = await start(client)
        assert "a stored App that cannot be used" in page and "rekey" in page and "Forget the App" in page
        assert isinstance(await forge_for_app(app).current(), FakeForge)  # type: ignore[attr-defined]
    assert "the stored GitHub App is not usable" in caplog.text and "rekey" in caplog.text


@pytest.mark.security
async def test_callback_only_from_loopback_without_an_admin_session(
    client: Client, exchange: list[httpx.Request]
) -> None:
    _, state = await start(client)
    remote = await callback(
        client, f"code=good-code&state={state}", scope_base={"client": ("203.0.113.9", 4321)}
    )
    assert remote.status_code == 403
    unix_socket = await callback(client, f"code=good-code&state={state}", scope_base={})
    assert unix_socket.status_code == 403, "no client address is not proof of a local caller"
    assert exchange == []


@pytest.mark.security
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-For": "203.0.113.66"},
        {"X-Forwarded-Host": "coverset-demo.ngrok.app", "X-Forwarded-Proto": "https"},
        {"Forwarded": "for=203.0.113.66;proto=https"},
        {"Host": "coverset-demo.ngrok.app", "X-Forwarded-For": "203.0.113.66"},
        {"X-Real-IP": "203.0.113.66"},
        {"True-Client-IP": "203.0.113.66"},
        {"CF-Connecting-IP": "203.0.113.66"},
        {"Via": "1.1 proxy"},
        {"X-Client-IP": "203.0.113.66"},
        {"Fastly-Client-IP": "203.0.113.66"},
        {"X-Cluster-Client-IP": "203.0.113.66"},
        {"X-ProxyUser-Ip": "203.0.113.66"},
        {"X-Original-Forwarded-For": "203.0.113.66"},
    ],
)
async def test_callback_through_a_tunnel_is_refused(
    client: Client, app: Quart, exchange: list[httpx.Request], headers: dict[str, str]
) -> None:
    """A tunnel connects from 127.0.0.1 too: its Host or forwarding headers give it away."""
    _, state = await start(client)
    response = await callback(client, f"code=good-code&state={state}", headers=headers)
    finish = await client.get("/setup/github/finish", headers=headers, scope_base=LOCAL)
    if "Host" in headers:
        # Open mode does not even answer for the tunnel's host name (DNS-rebinding guard).
        assert (response.status_code, finish.status_code) == (421, 421)
    else:
        assert (response.status_code, finish.status_code) == (403, 403)
    assert exchange == [] and await stored_rows(app) == {}


@pytest.mark.security
def test_tunnel_public_url_without_admin_token_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="COVERSET_ADMIN_TOKEN"):
        Settings.for_tests(data_dir=tmp_path, public_url="https://coverset-demo.ngrok.app")


@pytest.mark.security
async def test_admin_session_replaces_the_app_with_a_csrf_post(
    tmp_path: Path, admin_token: str, settings: Settings
) -> None:
    """With a token, the callback works through a tunnel for a signed-in admin, and replacing the
    stored App is a POST carrying the CSRF token."""
    settings = Settings.for_tests(
        database_url=settings.database_url,
        data_dir=tmp_path / "data",
        admin_token=admin_token,
        secret_key=SecretStr(SECRET_KEY),
        public_url="https://coverset-demo.ngrok.app",
    )
    async with database_with_app(settings, app_id=1, slug="old-app"):
        pass
    app = create_app(settings)
    seen = mock_github(app)
    tunnel = {"Host": "coverset-demo.ngrok.app", "X-Forwarded-For": "203.0.113.66"}
    async with app.test_app():
        client = app.test_client()
        anonymous = await client.get("/setup/github/callback?code=good-code&state=x", headers=tunnel)
        assert anonymous.status_code == 401
        login = await client.post("/login", json={"token": admin_token}, headers=tunnel)
        csrf = (await login.get_json())["csrf_token"]
        page = await (await client.get("/setup", headers=tunnel)).get_data(as_text=True)
        match = re.search(r"state=([^\"&]+)", page)
        assert match is not None
        arrival = await client.get(
            f"/setup/github/callback?code=good-code&state={html.unescape(match.group(1))}", headers=tunnel
        )
        assert arrival.status_code == 303
        refused = await client.get("/setup/github/finish", headers=tunnel)
        body = await refused.get_data(as_text=True)
        assert refused.status_code == 409 and 'name="overwrite"' in body and "old-app" in body and seen == []
        no_csrf = await client.post("/setup/github/finish", form={"overwrite": "1"}, headers=tunnel)
        assert no_csrf.status_code == 403 and seen == []
        accepted = await client.post(
            "/setup/github/finish", form={"overwrite": "1", "csrf_token": csrf}, headers=tunnel
        )
        assert accepted.status_code == 200, await accepted.get_data(as_text=True)
        forge = await forge_for_app(app).current()  # type: ignore[attr-defined]
        assert isinstance(forge, GitHubForge) and forge.app_id == 4242, "the new App, at once"
        rows = await stored_rows(app)
    assert AppCredentials.from_json(Vault(SECRET_KEY).open(APP_ROW, rows[APP_ROW])).slug == "coverset-test"
    assert len(seen) == 1


async def test_failed_exchange_is_reported(client: Client, app: Quart, exchange: list[httpx.Request]) -> None:
    _, state = await start(client)
    await callback(client, f"code=expired-code&state={state}")
    response = await client.get("/setup/github/finish", scope_base=LOCAL)
    assert response.status_code == 502 and "HTTP 404" in await response.get_data(as_text=True)
    _, state = await start(client)
    bad = await callback(client, f"code=..%2Fx&state={state}")
    assert bad.status_code == 400
    assert await stored_rows(app) == {}


@pytest.mark.security
async def test_no_secret_reaches_a_log_line(
    settings: Settings,
    app: Quart,
    client: Client,
    exchange: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every path that handles the App's secrets (exchange, sealing, the credential refresh, the
    forge rebuild, webhook verification, forgetting, an unusable row) logs none of them, checked on
    the raw records with the redacting filter switched off (it rewrites ``record.msg`` in place
    before caplog stores it, so with it on the test would only prove masking); and each loaded
    secret is registered with the redactor, so a line that quoted one would be masked."""
    monkeypatch.setattr(RedactingFilter, "filter", lambda self, record: True)
    caplog.set_level(logging.DEBUG)
    replica = create_app(settings)
    async with replica.test_app():
        assert (await finish_setup(client)).status_code == 200
        assert (await ping(client, CONVERSION["webhook_secret"])).status_code == 202
        assert (await ping(client, "wrong-secret-0123456789abcdef")).status_code == 403
        await forge_for_app(replica).current()  # type: ignore[attr-defined]
        async with client.session_transaction() as session:
            session["csrf"] = "known-token"
        await client.post("/setup/github/forget", form={"csrf_token": "known-token"}, scope_base=LOCAL)
    async with database_with_app(settings.model_copy(update={"secret_key": SecretStr("y" * 40)})):
        pass
    rotated = create_app(settings)
    async with rotated.test_app():
        await forge_for_app(rotated).current()  # type: ignore[attr-defined]
    messages = [record.getMessage() for record in caplog.records]
    assert any("GitHub credentials" in line for line in messages), "the paths did log"
    for line in [*messages, caplog.text]:
        for secret in SECRETS:
            assert secret not in line, (secret[:6], line[:200])
    for secret in (CONVERSION["client_secret"], CONVERSION["webhook_secret"], PEM):
        assert secret not in redact(f"value={secret} {secret}")
    installation, api_token = "ghs_" + "A1b2" * 10, "cst_" + "z9Y8" * 8  # the shapes, unregistered
    assert installation not in redact(f"token {installation}") and api_token not in redact(
        f"bearer {api_token}"
    )
    assert "MIIfakeKeyMaterial" not in redact(
        "-----BEGIN PRIVATE KEY-----\nMIIfakeKeyMaterial\n-----END PRIVATE KEY-----"
    )


def test_build_manifest() -> None:
    manifest = build_manifest("https://coverset.example.com/")
    assert manifest["hook_attributes"]["url"] == "https://coverset.example.com/webhooks/github"
    assert len(manifest["name"]) <= 34


@pytest.mark.security
def test_manifest_code_is_redacted_from_access_logs() -> None:
    line = '127.0.0.1:5000 - - "GET /setup/github/callback?code=0123abcd_ef-9&state=s HTTP/1.1" 303'
    assert (
        redact(line) == '127.0.0.1:5000 - - "GET /setup/github/callback?code=[REDACTED]&state=s HTTP/1.1" 303'
    )
    later = "GET /setup/github/callback?state=s&code=0123abcd HTTP/1.1"
    assert "0123abcd" not in redact(later)
