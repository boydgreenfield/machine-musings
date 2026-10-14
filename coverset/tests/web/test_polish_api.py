"""``coverset-ci wait`` with a ``cst_`` token: ``/v1/runs/<id>`` and its events for a token principal.

``authorize_repo`` accepts any principal the access guard resolved that may read the run's
repository (a repository token, a personal token, a signed-in user) with the full view; a token
for another repository is a 404 like a missing run, one that does not verify a 401. OIDC and the
anonymous rules for public repositories are unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from quart import Quart
from quart.typing import TestClientProtocol as Client

from coverset.db.models import Repo, RepoAccess, Run, RunEvent, User
from coverset.web import tokens

pytestmark = pytest.mark.security

SHA = "c" * 40


@pytest.fixture
def settings_overrides(admin_token: str) -> dict[str, Any]:
    return {"admin_token": admin_token}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def world(app: Quart) -> dict[str, Any]:
    """A private repository with a finished run and its events, another repository, a user."""
    async with app.extensions["db"].session() as session:
        first = Repo(forge="github", external_id="2001", full_name="acme/app", owner_id=7, private=True)
        second = Repo(forge="github", external_id="2002", full_name="acme/lib", owner_id=7, private=True)
        user = User(forge="github", external_id="31", login="mona")
        session.add_all([first, second, user])
        await session.flush()
        session.add(RepoAccess(user_id=user.id, repo_id=first.id, permission="read"))
        run = Run(
            repo_id=first.id,
            head_sha=SHA,
            trigger="pull_request",
            status="completed",
            verdict="failure",
            reason="1 test failed",
        )
        session.add(run)
        await session.flush()
        session.add_all(
            [
                RunEvent(run_id=run.id, seq=0, kind="progress", payload={"done": 0, "total": 2}),
                RunEvent(run_id=run.id, seq=1, kind="log", payload={"line": "collecting"}),
                RunEvent(run_id=run.id, seq=2, kind="verdict", payload={"conclusion": "failure"}),
                RunEvent(run_id=run.id, seq=3, kind="done", payload={}),
            ]
        )
        now = app.extensions["settings"].now()
        raw = {}
        for name, fields in {
            "repo": {"kind": "repo", "scopes": ["read"], "repo_id": first.id},
            "upload": {"kind": "repo", "scopes": ["upload"], "repo_id": first.id},
            "personal": {"kind": "personal", "scopes": ["read"], "user_id": user.id},
            "elsewhere": {"kind": "repo", "scopes": ["read", "upload"], "repo_id": second.id},
        }.items():
            raw[name], _row = await tokens.create(session, name=name, now=now, **fields)
        await session.commit()
        return {"run": run.id, "tokens": raw}


async def test_a_repository_token_follows_its_run(app: Quart, client: Client) -> None:
    w = await world(app)
    run_id = w["run"]
    for name in ("repo", "upload", "personal"):
        token = w["tokens"][name]
        detail = await client.get(f"/v1/runs/{run_id}", headers=bearer(token))
        assert detail.status_code == 200, name
        body = await detail.get_json()
        assert body["conclusion"] == "failure" and "1 test failed" in body["summary"], "the full view"
        polled = await client.get(f"/v1/runs/{run_id}/events?since=-1", headers=bearer(token))
        assert polled.status_code == 200, name
        events = await polled.get_json()
        assert events["done"] is True
        assert [e["kind"] for e in events["events"]] == ["progress", "log", "verdict", "done"], (
            "a reader gets the log events the anonymous view leaves out"
        )
        stream = await client.get(
            f"/v1/runs/{run_id}/events", headers={**bearer(token), "Accept": "text/event-stream"}
        )
        assert stream.status_code == 200 and stream.mimetype == "text/event-stream", name
        text = await stream.get_data(as_text=True)
        assert "event: verdict" in text and "event: done" in text
        data = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
        assert {"conclusion": "failure"} in data


async def test_a_token_for_another_repository_is_404(app: Quart, client: Client) -> None:
    w = await world(app)
    token = w["tokens"]["elsewhere"]
    for path in (f"/v1/runs/{w['run']}", f"/v1/runs/{w['run']}/events?since=-1"):
        response = await client.get(path, headers=bearer(token))
        assert response.status_code == 404 and await response.get_json() == {"error": "not_found"}, path
    # the same answer as a run that does not exist, with a token that may read it
    missing = await client.get("/v1/runs/run_nope", headers=bearer(w["tokens"]["repo"]))
    assert missing.status_code == 404 and await missing.get_json() == {"error": "not_found"}


async def test_a_bad_token_is_401_and_no_token_sees_nothing_private(app: Quart, client: Client) -> None:
    w = await world(app)
    bad = "cst_notthere_" + "z" * 40
    assert tokens.looks_like_token(bad), "well formed, so the cst_ path (not OIDC) refuses it"
    for path in (f"/v1/runs/{w['run']}", f"/v1/runs/{w['run']}/events?since=-1"):
        response = await client.get(path, headers=bearer(bad))
        assert response.status_code == 401 and await response.get_json() == {"error": "invalid_token"}, path
        anonymous = await client.get(path)
        assert anonymous.status_code == 404, "a private repository is invisible without credentials"
