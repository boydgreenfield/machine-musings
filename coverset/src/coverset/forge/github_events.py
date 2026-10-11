"""Parse GitHub webhook payloads into :class:`~coverset.forge.base.ForgeEvent` values.

Pure functions over the decoded JSON, shared by the GitHub adapter and the fake forge (whose
fixtures are GitHub-shaped). Repositories are identified by ``repository.id``; ``clone_url``,
``ssh_url`` and friends are never read. Every SHA is checked against ``^[0-9a-f]{40}$``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from coverset.forge.base import (
    ForgeEvent,
    ForgeEventError,
    ForgeEventKind,
    PullRef,
    RepoRef,
    check_full_name,
    check_sha,
    header,
)

ALLOWED_EVENTS = frozenset(
    {
        "pull_request",
        "push",
        "check_suite",
        "check_run",
        "installation",
        "installation_repositories",
        "repository",
        "ping",
    }
)
"""``X-GitHub-Event`` values the webhook route accepts (others are recorded and ignored)."""

ZERO_SHA = "0" * 40
DELIVERY_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")

_PR_ACTIONS: dict[str, ForgeEventKind] = {
    "opened": "pr_opened",
    "synchronize": "pr_sync",
    "reopened": "pr_reopened",
    "ready_for_review": "pr_ready",
}


def delivery_id(headers: Mapping[str, str]) -> str:
    """The ``X-GitHub-Delivery`` GUID; raises :class:`ForgeEventError` when missing or odd."""
    value = header(headers, "X-GitHub-Delivery") or ""
    if not DELIVERY_RE.fullmatch(value):
        raise ForgeEventError("missing or malformed X-GitHub-Delivery header")
    return value


def event_name(headers: Mapping[str, str]) -> str:
    """The ``X-GitHub-Event`` header (empty string when absent)."""
    return (header(headers, "X-GitHub-Event") or "").strip()


def decode_payload(raw_body: bytes) -> dict[str, Any]:
    """Decode a JSON object payload; raises :class:`ForgeEventError` for anything else."""
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ForgeEventError("payload is not JSON") from exc
    except RecursionError:
        raise ForgeEventError("payload is nested too deeply") from None
    if not isinstance(payload, dict):
        raise ForgeEventError("payload is not a JSON object")
    return payload


def parse_github_event(headers: Mapping[str, str], raw_body: bytes) -> ForgeEvent:
    """Parse one (already verified) GitHub delivery."""
    delivery = delivery_id(headers)
    name = event_name(headers)
    payload = decode_payload(raw_body)
    action = payload.get("action") if isinstance(payload.get("action"), str) else None
    base = {
        "delivery_id": delivery,
        "event_name": name,
        "action": action,
        "installation_id": _installation_id(payload),
        "sender": _login(payload.get("sender")),
        "payload": payload,
    }
    parser = _PARSERS.get(name)
    if parser is None:
        return ForgeEvent(kind="ignore", reason=f"event {name or '(none)'} is not handled", **base)
    return parser(payload, base)


def _obj(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ForgeEventError(f"payload has no {what} object")
    return value


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ForgeEventError(f"{what} is not an integer")
    return value


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _login(value: object) -> str | None:
    return _str(value.get("login")) if isinstance(value, Mapping) else None


def _installation_id(payload: Mapping[str, Any]) -> int | None:
    installation = payload.get("installation")
    return _opt_int(installation.get("id")) if isinstance(installation, Mapping) else None


def repo_ref(repository: object, installation_id: int | None = None) -> RepoRef:
    """A :class:`RepoRef` from a ``repository`` object (id, full_name, owner, private, default branch)."""
    repo = _obj(repository, "repository")
    owner = repo.get("owner")
    default_branch = _str(repo.get("default_branch")) or _str(repo.get("master_branch")) or "main"
    return RepoRef(
        external_id=_int(repo.get("id"), "repository.id"),
        full_name=check_full_name(_str(repo.get("full_name")) or ""),
        owner_id=_opt_int(owner.get("id")) if isinstance(owner, Mapping) else None,
        private=repo.get("private") is not False,
        default_branch=default_branch,
        installation_id=installation_id,
    )


def _ignore(base: dict[str, Any], reason: str, repo: RepoRef | None = None) -> ForgeEvent:
    return ForgeEvent(kind="ignore", reason=reason, repo=repo, **base)


def _pull_request(payload: Mapping[str, Any], base: dict[str, Any]) -> ForgeEvent:
    repo = repo_ref(payload.get("repository"), base["installation_id"])
    kind = _PR_ACTIONS.get(base["action"] or "")
    if kind is None:
        return _ignore(base, f"pull_request.{base['action']} is not handled", repo)
    pr = _obj(payload.get("pull_request"), "pull_request")
    head = _obj(pr.get("head"), "pull_request.head")
    base_side = _obj(pr.get("base"), "pull_request.base")
    head_repo = head.get("repo")
    head_repo_id = _opt_int(head_repo.get("id")) if isinstance(head_repo, Mapping) else None
    from_fork = head_repo_id is None or head_repo_id != repo.external_id
    association = _str(pr.get("author_association")) or "NONE"
    pull = PullRef(
        number=_int(pr.get("number", payload.get("number")), "pull_request.number"),
        head_sha=check_sha(head.get("sha"), "pull_request.head.sha"),
        base_sha=check_sha(base_side.get("sha"), "pull_request.base.sha"),
        head_ref=_str(head.get("ref")),
        base_ref=_str(base_side.get("ref")),
        title=(_str(pr.get("title")) or "")[:1000],
        author=_login(pr.get("user")),
        author_association=association,
        from_fork=from_fork,
        draft=pr.get("draft") is True,
    )
    return ForgeEvent(
        kind=kind,
        repo=repo,
        pull=pull,
        head_sha=pull.head_sha,
        base_sha=pull.base_sha,
        ref=pull.head_ref,
        author_association=association,
        from_fork=from_fork,
        draft=pull.draft,
        **base,
    )


def _push(payload: Mapping[str, Any], base: dict[str, Any]) -> ForgeEvent:
    repo = repo_ref(payload.get("repository"), base["installation_id"])
    ref = _str(payload.get("ref")) or ""
    if ref != f"refs/heads/{repo.default_branch}":
        return _ignore(base, f"push to {ref or '(no ref)'} is not the default branch", repo)
    if payload.get("deleted") is True:
        return _ignore(base, "default branch deleted", repo)
    after = check_sha(payload.get("after"), "push.after")
    before = _str(payload.get("before"))
    before_sha = check_sha(before, "push.before") if before and before != ZERO_SHA else None
    head_commit = payload.get("head_commit")
    message = _str(head_commit.get("message")) if isinstance(head_commit, Mapping) else None
    return ForgeEvent(
        kind="push",
        repo=repo,
        head_sha=after,
        base_sha=before_sha,
        ref=ref,
        head_commit_message=(message or "")[:1000] or None,
        **base,
    )


def _check_run(payload: Mapping[str, Any], base: dict[str, Any]) -> ForgeEvent:
    repo = repo_ref(payload.get("repository"), base["installation_id"])
    action = base["action"]
    if action not in ("rerequested", "requested_action"):
        return _ignore(base, f"check_run.{action} is not handled", repo)
    run = _obj(payload.get("check_run"), "check_run")
    common = {
        "repo": repo,
        "head_sha": check_sha(run.get("head_sha"), "check_run.head_sha"),
        "check_run_id": _opt_int(run.get("id")),
        "check_external_id": (_str(run.get("external_id")) or None),
        **base,
    }
    if action == "rerequested":
        return ForgeEvent(kind="rerun", **common)
    requested = _obj(payload.get("requested_action"), "requested_action")
    identifier = _str(requested.get("identifier"))
    if not identifier:
        raise ForgeEventError("requested_action.identifier is missing")
    return ForgeEvent(kind="action_button", action_identifier=identifier[:64], **common)


def _check_suite(payload: Mapping[str, Any], base: dict[str, Any]) -> ForgeEvent:
    repo = repo_ref(payload.get("repository"), base["installation_id"])
    action = base["action"]
    if action != "rerequested":
        # check_suite.requested arrives on every push; pull_request and push events drive runs in v1.
        return _ignore(base, f"check_suite.{action} is not handled", repo)
    suite = _obj(payload.get("check_suite"), "check_suite")
    return ForgeEvent(
        kind="rerun", repo=repo, head_sha=check_sha(suite.get("head_sha"), "check_suite.head_sha"), **base
    )


def _account_repos(payload: Mapping[str, Any], key: str, installation_id: int | None) -> tuple[RepoRef, ...]:
    """Repos listed under ``key`` of an installation event (they carry no owner or default branch)."""
    installation = payload.get("installation")
    account = installation.get("account") if isinstance(installation, Mapping) else None
    owner_id = _opt_int(account.get("id")) if isinstance(account, Mapping) else None
    listed = payload.get(key)
    if not isinstance(listed, list):
        return ()
    refs = []
    for item in listed:
        if not isinstance(item, Mapping):
            continue
        refs.append(
            RepoRef(
                external_id=_int(item.get("id"), f"{key}[].id"),
                full_name=check_full_name(_str(item.get("full_name")) or ""),
                owner_id=owner_id,
                private=item.get("private") is not False,
                installation_id=installation_id,
            )
        )
    return tuple(refs)


def _installation(payload: Mapping[str, Any], base: dict[str, Any]) -> ForgeEvent:
    action = base["action"]
    installation_id = base["installation_id"]
    repos = _account_repos(payload, "repositories", installation_id)
    if action in ("created", "unsuspend", "new_permissions_accepted"):
        return ForgeEvent(kind="install", repos_added=repos, **base)
    if action in ("deleted", "suspend"):
        return ForgeEvent(kind="uninstall", repos_removed=repos, **base)
    return _ignore(base, f"installation.{action} is not handled")


def _installation_repositories(payload: Mapping[str, Any], base: dict[str, Any]) -> ForgeEvent:
    installation_id = base["installation_id"]
    added = _account_repos(payload, "repositories_added", installation_id)
    removed = _account_repos(payload, "repositories_removed", installation_id)
    if base["action"] == "added":
        return ForgeEvent(kind="install", repos_added=added, **base)
    if base["action"] == "removed":
        return ForgeEvent(kind="uninstall", repos_removed=removed, **base)
    return _ignore(base, f"installation_repositories.{base['action']} is not handled")


_VISIBILITY_ACTIONS = {"privatized": True, "publicized": False}


def _repository(payload: Mapping[str, Any], base: dict[str, Any]) -> ForgeEvent:
    """``repository.privatized``/``publicized``: the repo's new privacy (other actions ignored)."""
    repo = repo_ref(payload.get("repository"), base["installation_id"])
    private = _VISIBILITY_ACTIONS.get(base["action"] or "")
    if private is None:
        return _ignore(base, f"repository.{base['action']} is not handled", repo)
    return ForgeEvent(kind="repo_visibility", repo=replace(repo, private=private), **base)


def _ping(payload: Mapping[str, Any], base: dict[str, Any]) -> ForgeEvent:
    return _ignore(base, "ping")


_PARSERS: dict[str, Callable[[Mapping[str, Any], dict[str, Any]], ForgeEvent]] = {
    "pull_request": _pull_request,
    "push": _push,
    "check_run": _check_run,
    "check_suite": _check_suite,
    "installation": _installation,
    "installation_repositories": _installation_repositories,
    "repository": _repository,
    "ping": _ping,
}
