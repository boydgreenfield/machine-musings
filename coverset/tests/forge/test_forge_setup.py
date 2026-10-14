"""``/setup``: the manifest form, state checks, the mocked code exchange, 0600 files, overwrite
refusal, tunnel refusal and the code-free finish step."""

from __future__ import annotations

import html
import json
import re
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from quart import Quart
from quart.typing import TestClientProtocol as Client

from coverset.app import create_app
from coverset.config import Settings
from coverset.forge.github import API_VERSION
from coverset.forge.manifest import build_manifest, merge_env
from coverset.logging import redact
from coverset.web.setup import ENV_FILE_CONFIG, TRANSPORT_EXTENSION

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----\n"
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


@pytest.fixture
def settings_overrides(tmp_path: Path) -> dict[str, Any]:
    return {"github_app_private_key_path": tmp_path / "secrets" / "github-app.pem"}


@pytest.fixture
def exchange(app: Quart, tmp_path: Path) -> list[httpx.Request]:
    """Mock GitHub's manifest conversion endpoint and point the callback's .env at tmp_path."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path == "/app-manifests/good-code/conversions":
            return httpx.Response(201, json=CONVERSION)
        return httpx.Response(404, json={"message": "Not Found"})

    app.extensions[TRANSPORT_EXTENSION] = httpx.MockTransport(handler)
    app.config[ENV_FILE_CONFIG] = str(tmp_path / ".env")
    return seen


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
async def test_callback_writes_env_and_key_with_mode_0600(
    client: Client, app: Quart, exchange: list[httpx.Request], tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    env.write_text("COVERSET_ADMIN_TOKEN=keep-me-0123456789abcdef0123456789\n")
    _, state = await start(client)
    arrival = await callback(client, f"code=good-code&state={state}")
    assert arrival.status_code == 303, "the code moves into the session first"
    location = arrival.headers["Location"]
    assert location.endswith("/setup/github/finish") and "good-code" not in location
    assert arrival.headers["Cache-Control"] == "no-store" and exchange == []
    response = await client.get(location, scope_base=LOCAL)
    page = await response.get_data(as_text=True)
    assert response.status_code == 200, page
    assert "GitHub App created" in page and "https://github.com/apps/coverset-test/installations/new" in page
    assert CONVERSION["client_secret"] not in page and "BEGIN RSA" not in page
    (request,) = exchange
    assert request.headers["X-GitHub-Api-Version"] == API_VERSION
    key = Path(app.extensions["settings"].github_app_private_key_path)
    assert key.read_text() == PEM
    for path in (key, env):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    text = env.read_text()
    assert "COVERSET_ADMIN_TOKEN=keep-me-0123456789abcdef0123456789" in text
    assert "COVERSET_GITHUB_APP_ID=4242" in text
    assert f"COVERSET_GITHUB_WEBHOOK_SECRET={CONVERSION['webhook_secret']}" in text
    assert f"COVERSET_GITHUB_APP_PRIVATE_KEY_PATH={key}" in text
    assert "COVERSET_ALLOWED_OWNERS=coverset-demo" in text

    replay = await callback(client, f"code=good-code&state={state}")
    assert replay.status_code == 400, "the state is single use"
    again = await client.get("/setup/github/finish", scope_base=LOCAL)
    assert again.status_code == 400 and len(exchange) == 1, "the code is single use"


@pytest.mark.security
async def test_bad_or_missing_state_is_400(client: Client, exchange: list[httpx.Request]) -> None:
    await start(client)
    for query in ("code=good-code&state=wrong", "code=good-code", "state="):
        response = await callback(client, query)
        assert response.status_code == 400, query
    assert (await client.get("/setup/github/finish", scope_base=LOCAL)).status_code == 400
    assert exchange == []


@pytest.mark.security
async def test_existing_credentials_cannot_be_replaced_in_open_mode(
    client: Client, app: Quart, exchange: list[httpx.Request]
) -> None:
    """``overwrite`` always needs an admin session; a query parameter no longer grants it."""
    key = Path(app.extensions["settings"].github_app_private_key_path)
    key.parent.mkdir(parents=True)
    key.write_text("old key")
    _, state = await start(client)
    arrival = await callback(client, f"code=good-code&state={state}&overwrite=1")
    refused = await client.get(arrival.headers["Location"], scope_base=LOCAL)
    page = await refused.get_data(as_text=True)
    assert refused.status_code == 409 and "Credentials already stored" in page
    assert "good-code" not in page and 'name="overwrite"' not in page and "COVERSET_ADMIN_TOKEN" in page
    async with client.session_transaction() as session:
        session["csrf"] = "known-token"
    forced = await client.post(
        "/setup/github/finish", form={"overwrite": "1", "csrf_token": "known-token"}, scope_base=LOCAL
    )
    assert forced.status_code == 403
    assert exchange == [] and key.read_text() == "old key"


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
    client: Client, exchange: list[httpx.Request], tmp_path: Path, headers: dict[str, str]
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
    assert exchange == [] and not (tmp_path / ".env").exists()


@pytest.mark.security
def test_tunnel_public_url_without_admin_token_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="COVERSET_ADMIN_TOKEN"):
        Settings.for_tests(data_dir=tmp_path, public_url="https://coverset-demo.ngrok.app")


@pytest.mark.security
async def test_admin_session_replaces_credentials_with_a_csrf_post(
    tmp_path: Path, admin_token: str, settings: Settings
) -> None:
    """With a token, the callback works through a tunnel for a signed-in admin, and replacing
    stored credentials is a POST carrying the CSRF token."""
    settings = Settings.for_tests(
        database_url=settings.database_url,
        data_dir=tmp_path / "data",
        admin_token=admin_token,
        public_url="https://coverset-demo.ngrok.app",
        github_app_private_key_path=tmp_path / "secrets" / "github-app.pem",
    )
    app = create_app(settings)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=CONVERSION)

    app.extensions[TRANSPORT_EXTENSION] = httpx.MockTransport(handler)
    app.config[ENV_FILE_CONFIG] = str(tmp_path / ".env")
    key = tmp_path / "secrets" / "github-app.pem"
    key.parent.mkdir(parents=True)
    key.write_text("old key")
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
        assert refused.status_code == 409 and 'name="overwrite"' in body and seen == []
        no_csrf = await client.post("/setup/github/finish", form={"overwrite": "1"}, headers=tunnel)
        assert no_csrf.status_code == 403 and seen == []
        accepted = await client.post(
            "/setup/github/finish", form={"overwrite": "1", "csrf_token": csrf}, headers=tunnel
        )
        assert accepted.status_code == 200, await accepted.get_data(as_text=True)
    assert key.read_text() == PEM and len(seen) == 1


async def test_failed_exchange_is_reported(client: Client, exchange: list[httpx.Request]) -> None:
    _, state = await start(client)
    await callback(client, f"code=expired-code&state={state}")
    response = await client.get("/setup/github/finish", scope_base=LOCAL)
    assert response.status_code == 502 and "HTTP 404" in await response.get_data(as_text=True)
    _, state = await start(client)
    bad = await callback(client, f"code=..%2Fx&state={state}")
    assert bad.status_code == 400


@pytest.mark.security
def test_manifest_code_is_redacted_from_access_logs() -> None:
    line = '127.0.0.1:5000 - - "GET /setup/github/callback?code=0123abcd_ef-9&state=s HTTP/1.1" 303'
    assert (
        redact(line) == '127.0.0.1:5000 - - "GET /setup/github/callback?code=[REDACTED]&state=s HTTP/1.1" 303'
    )
    later = "GET /setup/github/callback?state=s&code=0123abcd HTTP/1.1"
    assert "0123abcd" not in redact(later)


def test_build_manifest_and_merge_env() -> None:
    manifest = build_manifest("https://coverset.example.com/")
    assert manifest["hook_attributes"]["url"] == "https://coverset.example.com/webhooks/github"
    assert len(manifest["name"]) <= 34
    merged = merge_env(
        "# comment\nA=1\nCOVERSET_GITHUB_APP_ID=1\n", {"COVERSET_GITHUB_APP_ID": "2", "B": "x y"}
    )
    assert (
        merged
        == '# comment\nA=1\nCOVERSET_GITHUB_APP_ID=2\n\n# Written by coverset /setup (GitHub App manifest flow)\nB="x y"\n'
    )
