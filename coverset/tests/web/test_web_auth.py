"""Access control on the UI: an admin token locks every page, writes need the CSRF token."""

from __future__ import annotations

import re
from typing import Any

import pytest
from quart.typing import TestClientProtocol as Client
from test_web_pages import page_urls
from web_helpers import HX, Demo


@pytest.fixture
def settings_overrides(demo: Demo, admin_token: str) -> dict[str, Any]:
    return {**demo.overrides(), "admin_token": admin_token}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_every_page_needs_the_admin_token(client: Client, demo: Demo, admin_token: str) -> None:
    redirect = f"/repos/{demo.repo_id}/tests/by-nodeid?nodeid=tests/test_refund.py::test_refund_partial"
    for url in [*page_urls(demo), "/setup", f"/runs/{demo.pr1_run}/events", redirect]:
        anonymous = await client.get(url)
        assert anonymous.status_code == 401, url
        assert (await client.get(url, headers=HX)).status_code == 401, url
        assert (await client.get(url, headers=bearer("not-the-token"))).status_code == 401, url
    for url in page_urls(demo):
        assert (await client.get(url, headers=bearer(admin_token))).status_code == 200, url


async def test_static_files_stay_public(client: Client) -> None:
    for path in ("/static/app.css", "/static/app.js", "/static/vendor/htmx.min.js", "/static/favicon.svg"):
        assert (await client.get(path)).status_code == 200, path


async def login(client: Client, token: str) -> str:
    response = await client.post("/login", json={"token": token})
    assert response.status_code == 200
    return (await response.get_json())["csrf_token"]


async def test_quarantine_needs_csrf_with_a_cookie(client: Client, demo: Demo, admin_token: str) -> None:
    csrf = await login(client, admin_token)
    page = await (await client.get(f"/repos/{demo.repo_id}/tests")).get_data(as_text=True)
    assert f'"X-CSRF-Token": "{csrf}"' in page
    test_id = int(re.search(r'id="test-(\d+)"', page).group(1))  # type: ignore[union-attr]
    url = f"/repos/{demo.repo_id}/tests/{test_id}/quarantine"
    assert (await client.post(url, headers=HX, form={"quarantined": "1"})).status_code == 403
    wrong = {**HX, "X-CSRF-Token": csrf + "x"}
    assert (await client.post(url, headers=wrong, form={"quarantined": "1"})).status_code == 403
    ok = await client.post(url, headers={**HX, "X-CSRF-Token": csrf}, form={"quarantined": "1"})
    assert ok.status_code == 200 and 'aria-pressed="true"' in await ok.get_data(as_text=True)
    back = await client.post(url, headers={**HX, "X-CSRF-Token": csrf}, form={"quarantined": "0"})
    assert 'aria-pressed="false"' in await back.get_data(as_text=True)


async def test_quarantine_without_credentials_is_refused(client: Client, demo: Demo) -> None:
    response = await client.post(f"/repos/{demo.repo_id}/tests/1/quarantine", form={"quarantined": "1"})
    assert response.status_code == 401


async def test_bearer_writes_skip_csrf(client: Client, demo: Demo, admin_token: str) -> None:
    url = f"/repos/{demo.repo_id}/tests/1/quarantine"
    on = await client.post(url, headers={**HX, **bearer(admin_token)}, form={"quarantined": "1"})
    assert on.status_code == 200
    off = await client.post(url, headers={**HX, **bearer(admin_token)}, form={"quarantined": "0"})
    assert 'aria-pressed="false"' in await off.get_data(as_text=True)


async def test_quarantine_toggle_shown_to_admins(client: Client, demo: Demo, admin_token: str) -> None:
    html = await (await client.get(f"/repos/{demo.repo_id}/tests", headers=bearer(admin_token))).get_data(
        as_text=True
    )
    assert "toggle-q" in html
