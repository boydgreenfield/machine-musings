"""Access control: admin token via bearer or login cookie, public routes, CSRF."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from quart import Blueprint
from quart.typing import TestClientProtocol as Client

from coverset.app import MAX_CONTENT_LENGTH, create_app
from coverset.config import Settings
from coverset.web.auth import CSRF_HEADER, current_auth, is_admin, is_public, require_admin


@pytest.fixture
def settings_overrides(admin_token: str) -> dict[str, Any]:
    return {"admin_token": admin_token}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def probe_app(settings: Settings) -> Client:
    """A client for an app with stand-in views on public and protected paths reporting their auth."""
    app = create_app(settings)
    probes = Blueprint("probes", __name__)

    async def report(**_view_args: str) -> dict[str, str | bool | None]:
        return {"auth": current_auth(), "admin": is_admin()}

    for rule in ("/v1/verdict", "/v1/runs", "/v1/runs/<run_id>", "/v1/runs/<run_id>/events"):
        probes.add_url_rule(rule, view_func=report, methods=["GET"], endpoint=rule)
    probes.add_url_rule("/webhooks/github", view_func=report, methods=["POST"], endpoint="hook")
    app.register_blueprint(probes)
    return app.test_client()


async def login(client: Client, token: str) -> str:
    """Log in through the JSON form of ``POST /login`` and return the session's CSRF token."""
    response = await client.post("/login", json={"token": token})
    assert response.status_code == 200
    body = await response.get_json()
    assert body["ok"] is True
    return body["csrf_token"]


async def test_protected_route_requires_auth(client: Client, admin_token: str) -> None:
    anonymous = await client.get("/")
    assert anonymous.status_code == 401
    assert anonymous.headers["WWW-Authenticate"].startswith("Bearer")
    assert (await client.get("/", headers=bearer(admin_token))).status_code == 200
    assert (await client.get("/", headers=bearer(admin_token + "x"))).status_code == 401
    assert (await client.get("/", headers=bearer(""))).status_code == 401
    assert (await client.get("/", headers={"Authorization": f"Token {admin_token}"})).status_code == 401


async def test_wrong_bearer_never_falls_back_to_cookie(client: Client, admin_token: str) -> None:
    await login(client, admin_token)
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/", headers=bearer("wrong-token"))).status_code == 401


async def test_unauthorized_json_for_api_clients(client: Client) -> None:
    response = await client.get("/", headers={"Accept": "application/json"})
    assert response.status_code == 401
    assert await response.get_json() == {"error": "unauthorized"}


async def test_public_routes_need_no_auth(client: Client, settings: Settings) -> None:
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/login")).status_code == 200
    probe = probe_app(settings)
    for method, path in [
        ("GET", "/v1/verdict"),
        ("GET", "/v1/runs/run_1"),
        ("GET", "/v1/runs/run_1/events"),
        ("POST", "/webhooks/github"),
    ]:
        response = await probe.open(path, method=method)
        assert response.status_code == 200, path
        assert await response.get_json() == {"auth": "public", "admin": False}
    assert (await probe.get("/v1/runs")).status_code == 401


async def test_public_routes_see_admin_credentials(settings: Settings, admin_token: str) -> None:
    probe = probe_app(settings)
    response = await probe.get("/v1/runs/run_1", headers=bearer(admin_token))
    assert await response.get_json() == {"auth": "bearer", "admin": True}
    await login(probe, admin_token)
    response = await probe.get("/v1/runs/run_1/events")
    assert await response.get_json() == {"auth": "cookie", "admin": True}
    # A cookie write without the CSRF header stays a plain public request.
    response = await probe.post("/webhooks/github")
    assert await response.get_json() == {"auth": "public", "admin": False}


async def test_non_admin_bearer_reaches_self_authorizing_views(settings: Settings) -> None:
    """``coverset-ci wait`` sends a GitHub OIDC JWT; the guard leaves it for the view to verify."""
    probe = probe_app(settings)
    oidc = bearer("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJyZXBvOmFjbWUvYXBwIn0.c2ln")
    for path in ("/v1/verdict", "/v1/runs/run_1", "/v1/runs/run_1/events"):
        response = await probe.get(path, headers=oidc)
        assert response.status_code == 200, path
        assert await response.get_json() == {"auth": "public", "admin": False}
    assert (await probe.get("/v1/runs", headers=oidc)).status_code == 401
    assert (await probe.get("/", headers=oidc)).status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "public"),
    [
        ("POST", "/webhooks/github", True),
        ("GET", "/webhooks/github", False),
        ("GET", "/v1/verdict", True),
        ("POST", "/v1/verdict", False),
        ("GET", "/v1/runs", False),
        ("GET", "/v1/runs/run_1", True),
        ("HEAD", "/v1/runs/run_1", True),
        ("GET", "/v1/runs/run_1/events", True),
        ("POST", "/v1/runs/run_1", False),
        ("GET", "/v1/runs/run_1/cancel", False),
        ("GET", "/v1/runs/", False),
        ("GET", "/healthz", True),
        ("HEAD", "/healthz", True),
        ("POST", "/login", True),
        ("GET", "/static/app.css", True),
        ("GET", "/", False),
        ("POST", "/logout", False),
        ("GET", "/webhooks", False),
    ],
)
def test_is_public(method: str, path: str, public: bool) -> None:
    assert is_public(method, path) is public


async def test_form_login_sets_cookie_and_redirects(client: Client, admin_token: str) -> None:
    response = await client.post("/login", form={"token": admin_token, "next": "/repos/1"})
    assert response.status_code == 303
    assert response.headers["Location"] == "/repos/1"
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith("coverset_session=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert (await client.get("/")).status_code == 200


UNSAFE_NEXT = [
    "//evil.example/x",
    "https://evil.example/",
    "/\\evil.example",
    "/\t/evil.example",
    "/\n/evil.example",
    "/\r/evil.example",
    "/ /evil.example",
    "/\x7f/evil.example",
    "javascript:alert(1)",
    "evil.example",
    "",
]


@pytest.mark.parametrize("target", UNSAFE_NEXT)
async def test_login_redirect_stays_on_site(client: Client, admin_token: str, target: str) -> None:
    response = await client.post("/login", form={"token": admin_token, "next": target})
    assert response.status_code == 303
    assert response.headers["Location"] == "/"


@pytest.mark.parametrize("target", ["/\t/evil.example", "//evil.example"])
async def test_login_form_drops_unsafe_next(client: Client, target: str) -> None:
    response = await client.get("/login", query_string={"next": target})
    assert response.status_code == 200
    assert 'name="next" value="/"' in (await response.get_data(as_text=True))


@pytest.mark.parametrize("target", ["/repos/1", "/repos/1/files/a%09b.py?run=run_1#L3", "/%09/x"])
async def test_login_redirect_keeps_site_paths(client: Client, admin_token: str, target: str) -> None:
    response = await client.post("/login", form={"token": admin_token, "next": target})
    assert response.headers["Location"] == target


async def test_bad_login_rejected(client: Client, admin_token: str) -> None:
    assert (await client.post("/login", form={"token": "nope"})).status_code == 401
    assert (await client.post("/login", json={"token": admin_token[:-1]})).status_code == 401
    assert (await client.post("/login", json={"token": 123})).status_code == 401
    assert (await client.post("/login", json=["not", "an", "object"])).status_code == 401
    assert (await client.get("/")).status_code == 401


async def test_cookie_write_needs_csrf_header(client: Client, admin_token: str) -> None:
    csrf = await login(client, admin_token)
    assert (await client.post("/logout")).status_code == 403
    assert (await client.post("/logout", headers={CSRF_HEADER: "forged"})).status_code == 403
    response = await client.post("/logout", headers={CSRF_HEADER: csrf})
    assert response.status_code == 303
    assert (await client.get("/")).status_code == 401


async def test_bearer_write_needs_no_csrf(client: Client, admin_token: str) -> None:
    response = await client.post("/logout", headers=bearer(admin_token), json={})
    assert response.status_code == 200
    assert await response.get_json() == {"ok": True}


async def test_rotating_admin_token_invalidates_cookies(
    client: Client, settings: Settings, admin_token: str
) -> None:
    await login(client, admin_token)

    async def status_with_cookie(app_settings: Settings) -> int:
        other = create_app(app_settings).test_client()
        other.cookie_jar = client.cookie_jar
        return (await other.get("/")).status_code

    assert await status_with_cookie(settings) == 200
    assert await status_with_cookie(settings.model_copy(update={"admin_token": SecretStr("r" * 40)})) == 401


async def test_oversized_login_body_rejected(client: Client) -> None:
    size = MAX_CONTENT_LENGTH + 1
    response = await client.post("/login", data=b"x" * size, headers={"Content-Length": str(size)})
    assert response.status_code == 413


async def test_require_admin_blocks_public_paths(settings: Settings, admin_token: str) -> None:
    app = create_app(settings)
    extra = Blueprint("extra", __name__)

    @extra.post("/webhooks/admin-only")
    @require_admin
    async def admin_only() -> dict[str, str | None]:
        return {"auth": current_auth()}

    app.register_blueprint(extra)
    client = app.test_client()
    assert (await client.post("/webhooks/admin-only")).status_code == 401


async def test_token_mode_does_not_check_host(client: Client, admin_token: str) -> None:
    """With a token, a rebinding page has no credentials; deployments behind proxies keep working."""
    assert (await client.get("/", headers={"Host": "attacker.example"})).status_code == 401
    headers = {"Host": "10.0.0.5:8000", **bearer(admin_token)}
    assert (await client.get("/", headers=headers)).status_code == 200


class TestOpenMode:
    """No admin token (loopback only): reads are open, writes still need the CSRF header."""

    @pytest.fixture
    def settings_overrides(self) -> dict[str, Any]:
        return {}

    async def test_reads_open(self, client: Client) -> None:
        assert (await client.get("/")).status_code == 200

    async def test_login_disabled(self, client: Client, admin_token: str) -> None:
        assert (await client.get("/login")).status_code == 404
        assert (await client.post("/login", json={"token": admin_token})).status_code == 404

    @pytest.mark.parametrize("host", ["attacker.example", "attacker.example:8000", "10.0.0.5:8000"])
    async def test_foreign_host_rejected(self, client: Client, host: str) -> None:
        """A page that rebinds its DNS name to 127.0.0.1 must not reach coverset."""
        assert (await client.get("/", headers={"Host": host})).status_code == 421
        assert (await client.get("/healthz", headers={"Host": host})).status_code == 421
        async with client.session_transaction() as session:
            session["csrf"] = "known-token"
        response = await client.post("/logout", headers={"Host": host, CSRF_HEADER: "known-token"})
        assert response.status_code == 421
        rejected = await client.get("/v1/verdict", headers={"Host": host})
        assert await rejected.get_json() == {"error": "bad_host"}

    @pytest.mark.parametrize(
        "host", ["127.0.0.1:8000", "localhost", "LOCALHOST.:8000", "[::1]:8000", "dash.localhost:8000"]
    )
    async def test_loopback_hosts_accepted(self, client: Client, host: str) -> None:
        assert (await client.get("/", headers={"Host": host})).status_code == 200

    async def test_public_url_host_accepted(self, tmp_path: Path) -> None:
        settings = Settings.for_tests(data_dir=tmp_path, public_url="https://ci.example.org")
        client = create_app(settings).test_client()
        assert (await client.get("/healthz", headers={"Host": "ci.example.org"})).status_code == 200
        assert (await client.get("/healthz", headers={"Host": "other.example.org"})).status_code == 421

    async def test_writes_need_csrf(self, client: Client) -> None:
        assert (await client.post("/logout")).status_code == 403
        async with client.session_transaction() as session:
            session["csrf"] = "known-token"
        assert (await client.post("/logout", headers={CSRF_HEADER: "known-token"})).status_code == 303
