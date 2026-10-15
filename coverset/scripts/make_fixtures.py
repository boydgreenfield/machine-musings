"""Generate the signed GitHub webhook fixtures in ``fixtures/webhooks/`` from the seeded demo SHAs.

Each fixture is ``{"headers": {...}, "body": "<raw JSON exactly as signed>"}``: ``body`` is the
byte-exact payload, and ``X-Hub-Signature-256`` is its HMAC-SHA256 under the test secret
``coverset-test-secret`` (accepted only when ``COVERSET_ENV=test``; ``coverset replay`` re-signs
with the configured secret). Payloads follow the shapes of octokit's webhook examples, trimmed to
realistic but stable fields, for the demo repository ``coverset-demo/ledger`` (repository id
424242, owner id 777, installation 55).

SHAs come from ``data/demo.git.shas.json`` when ``scripts/seed_demo.py`` has written it, else from
the constants in ``tests/test_seed_demo.py``. Output is deterministic: delivery GUIDs are UUIDv5s of
the fixture name and every timestamp is fixed.

    uv run python scripts/make_fixtures.py           # (re)write fixtures/webhooks/*.json
    uv run python scripts/make_fixtures.py --check   # exit 1 if the files are stale
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "fixtures" / "webhooks"
SHAS_FILE = ROOT / "data" / "demo.git.shas.json"
SEED_TEST = ROOT / "tests" / "test_seed_demo.py"

TEST_SECRET = "coverset-test-secret"
OWNER_LOGIN = "coverset-demo"
OWNER_ID = 777
REPO_NAME = "ledger"
REPO_ID = 424242
INSTALLATION_ID = 55
APP_ID = 1001
APP_SLUG = "coverset-demo-app"
HOOK_ID = 480001
FORK_OWNER = "outside-contrib"
FORK_OWNER_ID = 9002
FORK_REPO_ID = 515151
MAINTAINER = "dana-maintainer"
MAINTAINER_ID = 9001
FORK_PR_NUMBER = 3
FORK_PR_TITLE = "Tidy interval merging"
APPROVAL_RUN_ID = "run_fixture_fork_pr3"
"""``check_run.external_id`` of the approval fixture: coverset sets it to the awaiting run's id."""
_NAMESPACE = uuid.UUID("6f1c3a52-1d53-4c55-9a77-0c0ffee0ab1e")
_T0 = "2026-10-01T12:00:00Z"
_API = "https://api.github.com"
_WEB = "https://github.com"
DEFAULT_PR_TITLES = {1: "Support partial refunds", 2: "Allow signups from company subdomains"}


def _seed_test_constants() -> dict[str, Any]:
    """``EXPECTED_COMMITS``/``EXPECTED_BRANCHES`` read from the test module's source (not executed)."""
    tree = ast.parse(SEED_TEST.read_text(encoding="utf-8"))
    wanted = {"EXPECTED_COMMITS", "EXPECTED_BRANCHES"}
    found: dict[str, Any] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in wanted:
            found[target.id] = ast.literal_eval(node.value)
    missing = wanted - found.keys()
    if missing:
        raise RuntimeError(f"{SEED_TEST} no longer defines {sorted(missing)}")
    return found


def load_shas() -> dict[str, Any]:
    """``{"commits": {...}, "branches": {...}, "prs": [...]}`` from the seed manifest or the test constants."""
    if SHAS_FILE.exists():
        return json.loads(SHAS_FILE.read_text(encoding="utf-8"))
    constants = _seed_test_constants()
    branches = dict(constants["EXPECTED_BRANCHES"])
    prs = [
        {"number": 1, "branch": "pr-1-partial-refunds", "title": DEFAULT_PR_TITLES[1]},
        {"number": 2, "branch": "pr-2-subdomain-signups", "title": DEFAULT_PR_TITLES[2]},
    ]
    for pr in prs:
        pr["head_sha"] = branches[pr["branch"]]
        pr["base_sha"] = branches["main"]
    return {"commits": dict(constants["EXPECTED_COMMITS"]), "branches": branches, "prs": prs}


def _user(login: str, user_id: int, kind: str = "User") -> dict[str, Any]:
    return {
        "login": login,
        "id": user_id,
        "node_id": f"U_{user_id:08d}",
        "avatar_url": f"https://avatars.githubusercontent.com/u/{user_id}?v=4",
        "html_url": f"{_WEB}/{login}",
        "url": f"{_API}/users/{login}",
        "type": kind,
        "site_admin": False,
    }


def _repository(
    full_owner: str = OWNER_LOGIN, owner_id: int = OWNER_ID, repo_id: int = REPO_ID, fork: bool = False
) -> dict[str, Any]:
    full_name = f"{full_owner}/{REPO_NAME}"
    return {
        "id": repo_id,
        "node_id": f"R_{repo_id:010d}",
        "name": REPO_NAME,
        "full_name": full_name,
        "private": False,
        "owner": _user(full_owner, owner_id, "User" if fork else "Organization"),
        "html_url": f"{_WEB}/{full_name}",
        "description": "Demo ledger package used by coverset",
        "fork": fork,
        "url": f"{_API}/repos/{full_name}",
        "git_url": f"git://github.com/{full_name}.git",
        "ssh_url": f"git@github.com:{full_name}.git",
        "clone_url": f"{_WEB}/{full_name}.git",
        "created_at": _T0,
        "updated_at": _T0,
        "pushed_at": _T0,
        "language": "Python",
        "visibility": "public",
        "default_branch": "main",
    }


def _installation() -> dict[str, Any]:
    return {"id": INSTALLATION_ID, "node_id": f"MDIzOkludGVncmF0aW9uSW5zdGFsbGF0aW9u{INSTALLATION_ID}"}


def _side(sha: str, ref: str, repo: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": f"{repo['owner']['login']}:{ref}",
        "ref": ref,
        "sha": sha,
        "user": repo["owner"],
        "repo": repo,
    }


def _pull_request(
    number: int,
    title: str,
    head: dict[str, Any],
    base: dict[str, Any],
    author: dict[str, Any],
    association: str,
) -> dict[str, Any]:
    full_name = f"{OWNER_LOGIN}/{REPO_NAME}"
    return {
        "url": f"{_API}/repos/{full_name}/pulls/{number}",
        "id": 3_000_000 + number,
        "node_id": f"PR_{number:08d}",
        "html_url": f"{_WEB}/{full_name}/pull/{number}",
        "number": number,
        "state": "open",
        "locked": False,
        "title": title,
        "user": author,
        "body": f"{title}.",
        "created_at": _T0,
        "updated_at": _T0,
        "closed_at": None,
        "merged_at": None,
        "draft": False,
        "head": head,
        "base": base,
        "author_association": association,
        "merged": False,
        "mergeable": None,
        "commits": 1,
        "additions": 24,
        "deletions": 3,
        "changed_files": 2,
    }


def _pr_event(action: str, pr: dict[str, Any], sender: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "action": action,
        "number": pr["number"],
        "pull_request": pr,
        **extra,
        "repository": _repository(),
        "organization": {"login": OWNER_LOGIN, "id": OWNER_ID},
        "sender": sender,
        "installation": _installation(),
    }


def _demo_pr(shas: dict[str, Any], number: int) -> dict[str, Any]:
    for pr in shas.get("prs", []):
        if pr.get("number") == number:
            return pr
    raise KeyError(f"PR #{number} missing from the demo SHA manifest")


def pr_opened(shas: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """PR #1 opened by a trusted member."""
    demo = _demo_pr(shas, 1)
    maintainer = _user(MAINTAINER, MAINTAINER_ID)
    repo = _repository()
    pr = _pull_request(
        1,
        demo.get("title") or DEFAULT_PR_TITLES[1],
        _side(demo["head_sha"], demo["branch"], repo),
        _side(demo["base_sha"], "main", repo),
        maintainer,
        "MEMBER",
    )
    return "pull_request", _pr_event("opened", pr, maintainer)


def pr_synchronize(shas: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """PR #2 received a new head commit."""
    demo = _demo_pr(shas, 2)
    maintainer = _user(MAINTAINER, MAINTAINER_ID)
    repo = _repository()
    pr = _pull_request(
        2,
        demo.get("title") or DEFAULT_PR_TITLES[2],
        _side(demo["head_sha"], demo["branch"], repo),
        _side(demo["base_sha"], "main", repo),
        maintainer,
        "MEMBER",
    )
    return "pull_request", _pr_event(
        "synchronize", pr, maintainer, before=shas["branches"]["main"], after=demo["head_sha"]
    )


def pr_opened_fork(shas: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """PR #3 from a fork by a first-time contributor (``author_association: NONE``).

    Its head is the demo's ``main-5`` commit, which exists in the demo mirror, so an approved run
    can archive it without colliding with the PR #1/#2 heads.
    """
    contributor = _user(FORK_OWNER, FORK_OWNER_ID)
    fork = _repository(FORK_OWNER, FORK_OWNER_ID, FORK_REPO_ID, fork=True)
    pr = _pull_request(
        FORK_PR_NUMBER,
        FORK_PR_TITLE,
        _side(shas["commits"]["main-5"], "tidy-intervals", fork),
        _side(shas["branches"]["main"], "main", _repository()),
        contributor,
        "NONE",
    )
    return "pull_request", _pr_event("opened", pr, contributor)


def push_main(shas: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A push to ``main`` (the last seeded commit)."""
    before, after = shas["commits"]["main-5"], shas["commits"]["main-6"]
    full_name = f"{OWNER_LOGIN}/{REPO_NAME}"
    commit = {
        "id": after,
        "tree_id": hashlib.sha1(after.encode()).hexdigest(),
        "distinct": True,
        "message": "Main commit 6",
        "timestamp": "2026-10-01T17:00:00Z",
        "url": f"{_WEB}/{full_name}/commit/{after}",
        "author": {"name": "coverset demo", "email": "demo@coverset.invalid", "username": MAINTAINER},
        "committer": {"name": "coverset demo", "email": "demo@coverset.invalid", "username": MAINTAINER},
        "added": [],
        "removed": [],
        "modified": ["src/ledger/__init__.py"],
    }
    payload = {
        "ref": "refs/heads/main",
        "before": before,
        "after": after,
        "repository": _repository(),
        "pusher": {"name": MAINTAINER, "email": "demo@coverset.invalid"},
        "organization": {"login": OWNER_LOGIN, "id": OWNER_ID},
        "sender": _user(MAINTAINER, MAINTAINER_ID),
        "installation": _installation(),
        "created": False,
        "deleted": False,
        "forced": False,
        "base_ref": None,
        "compare": f"{_WEB}/{full_name}/compare/{before[:12]}...{after[:12]}",
        "commits": [commit],
        "head_commit": commit,
    }
    return "push", payload


def _check_run(
    name: str, head_sha: str, pull_requests: list[dict[str, Any]], run_id: int, external_id: str = ""
) -> dict[str, Any]:
    full_name = f"{OWNER_LOGIN}/{REPO_NAME}"
    return {
        "id": run_id,
        "node_id": f"CR_{run_id:010d}",
        "head_sha": head_sha,
        "external_id": external_id,
        "url": f"{_API}/repos/{full_name}/check-runs/{run_id}",
        "html_url": f"{_WEB}/{full_name}/runs/{run_id}",
        "details_url": "http://127.0.0.1:8000/",
        "status": "completed",
        "conclusion": "neutral",
        "started_at": _T0,
        "completed_at": _T0,
        "output": {"title": name, "summary": "", "text": None, "annotations_count": 0},
        "name": name,
        "check_suite": {"id": 7_000_000 + run_id % 1000, "head_branch": None, "head_sha": head_sha},
        "app": {
            "id": APP_ID,
            "slug": APP_SLUG,
            "name": "coverset demo app",
            "owner": _user(OWNER_LOGIN, OWNER_ID),
        },
        "pull_requests": pull_requests,
    }


def check_run_rerequested(shas: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Someone pressed "Re-run" on PR #1's coverset check."""
    demo = _demo_pr(shas, 1)
    pulls = [
        {
            "url": f"{_API}/repos/{OWNER_LOGIN}/{REPO_NAME}/pulls/1",
            "id": 3_000_001,
            "number": 1,
            "head": {
                "ref": demo["branch"],
                "sha": demo["head_sha"],
                "repo": {"id": REPO_ID, "name": REPO_NAME},
            },
            "base": {"ref": "main", "sha": demo["base_sha"], "repo": {"id": REPO_ID, "name": REPO_NAME}},
        }
    ]
    payload = {
        "action": "rerequested",
        "check_run": _check_run("coverset", demo["head_sha"], pulls, 900_001),
        "repository": _repository(),
        "organization": {"login": OWNER_LOGIN, "id": OWNER_ID},
        "sender": _user(MAINTAINER, MAINTAINER_ID),
        "installation": _installation(),
    }
    return "check_run", payload


def check_run_requested_action(shas: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A maintainer pressed "Approve run" on the fork PR's awaiting-approval check."""
    payload = {
        "action": "requested_action",
        "check_run": _check_run(
            "coverset: awaiting approval", shas["commits"]["main-5"], [], 900_002, APPROVAL_RUN_ID
        ),
        "requested_action": {"identifier": "approve-run"},
        "repository": _repository(),
        "organization": {"login": OWNER_LOGIN, "id": OWNER_ID},
        "sender": _user(MAINTAINER, MAINTAINER_ID),
        "installation": _installation(),
    }
    return "check_run", payload


def installation_created(shas: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The App was installed on the demo organization, selecting the ledger repository."""
    owner = _user(OWNER_LOGIN, OWNER_ID, "Organization")
    payload = {
        "action": "created",
        "installation": {
            **_installation(),
            "account": owner,
            "repository_selection": "selected",
            "app_id": APP_ID,
            "app_slug": APP_SLUG,
            "target_id": OWNER_ID,
            "target_type": "Organization",
            "permissions": {
                "checks": "write",
                "contents": "read",
                "issues": "write",
                "metadata": "read",
                "pull_requests": "write",
            },
            "events": ["check_run", "check_suite", "pull_request", "push"],
            "created_at": _T0,
            "updated_at": _T0,
        },
        "repositories": [
            {
                "id": REPO_ID,
                "node_id": f"R_{REPO_ID:010d}",
                "name": REPO_NAME,
                "full_name": f"{OWNER_LOGIN}/{REPO_NAME}",
                "private": False,
            }
        ],
        "requester": None,
        "sender": _user(MAINTAINER, MAINTAINER_ID),
    }
    return "installation", payload


def ping(shas: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """GitHub's ping after the App's webhook is configured."""
    payload = {
        "zen": "Design for failure.",
        "hook_id": HOOK_ID,
        "hook": {
            "type": "App",
            "id": HOOK_ID,
            "name": "web",
            "active": True,
            "events": ["check_run", "check_suite", "pull_request", "push"],
            "config": {
                "content_type": "json",
                "insecure_ssl": "0",
                "url": "http://127.0.0.1:8000/webhooks/github",
            },
            "updated_at": _T0,
            "created_at": _T0,
            "app_id": APP_ID,
        },
    }
    return "ping", payload


BUILDERS: dict[str, Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]] = {
    "pr_opened": pr_opened,
    "pr_synchronize": pr_synchronize,
    "pr_opened_fork": pr_opened_fork,
    "push_main": push_main,
    "check_run_rerequested": check_run_rerequested,
    "check_run_requested_action": check_run_requested_action,
    "installation_created": installation_created,
    "ping": ping,
}
"""Fixture name → payload builder (the file is ``fixtures/webhooks/<name>.json``)."""


def sign(secret: str, body: str) -> str:
    """``sha256=<hex>`` over the UTF-8 bytes of ``body``."""
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def build_fixture(name: str, shas: dict[str, Any], secret: str = TEST_SECRET) -> dict[str, Any]:
    """One fixture: headers (with the signature) and the exact signed body."""
    event, payload = BUILDERS[name](shas)
    body = json.dumps(payload, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "GitHub-Hookshot/7b4c2a1",
        "X-GitHub-Delivery": str(uuid.uuid5(_NAMESPACE, name)),
        "X-GitHub-Event": event,
        "X-GitHub-Hook-ID": str(HOOK_ID),
        "X-GitHub-Hook-Installation-Target-ID": str(APP_ID),
        "X-GitHub-Hook-Installation-Target-Type": "integration",
        "X-Hub-Signature-256": sign(secret, body),
    }
    return {"headers": headers, "body": body}


def build_all(shas: dict[str, Any] | None = None) -> dict[str, str]:
    """``{file name: file text}`` for every fixture."""
    shas = shas if shas is not None else load_shas()
    return {f"{name}.json": json.dumps(build_fixture(name, shas), indent=2) + "\n" for name in BUILDERS}


def main(argv: list[str] | None = None) -> int:
    """Write (or with ``--check`` verify) the fixtures."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="output directory")
    parser.add_argument("--check", action="store_true", help="exit 1 if any fixture differs")
    args = parser.parse_args(argv)
    files = build_all()
    stale = [
        name
        for name, text in files.items()
        if not (args.out / name).exists() or (args.out / name).read_text(encoding="utf-8") != text
    ]
    if args.check:
        for name in stale:
            print(f"stale: {args.out / name}")
        return 1 if stale else 0
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (args.out / name).write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} fixtures to {args.out} ({len(stale)} changed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
