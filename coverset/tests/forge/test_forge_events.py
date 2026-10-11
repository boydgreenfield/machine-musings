"""Signatures, the secret policy, payload parsing for every fixture, and fixture determinism."""

from __future__ import annotations

import importlib.util
import json
from types import ModuleType

import pytest
from forge_helpers import DEMO_OWNER_ID, DEMO_REPO_ID, FIXTURE_NAMES, ROOT, load_fixture
from pydantic import SecretStr

from coverset.config import Settings
from coverset.forge.base import ForgeEventError, MirrorCredentials, PullRef
from coverset.forge.github_events import ALLOWED_EVENTS, decode_payload, parse_github_event
from coverset.forge.signing import TEST_WEBHOOK_SECRET, sign_body, verify_signature, webhook_secret

GITHUB_VECTOR_SECRET = "It's a Secret to Everybody"
GITHUB_VECTOR_PAYLOAD = b"Hello, World!"
GITHUB_VECTOR_SIGNATURE = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"


def test_github_published_hmac_vector() -> None:
    assert sign_body(GITHUB_VECTOR_SECRET, GITHUB_VECTOR_PAYLOAD) == GITHUB_VECTOR_SIGNATURE
    headers = {"x-hub-signature-256": GITHUB_VECTOR_SIGNATURE}
    assert verify_signature(GITHUB_VECTOR_SECRET, headers, GITHUB_VECTOR_PAYLOAD)


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        "sha256=" + "0" * 64,
        GITHUB_VECTOR_SIGNATURE.upper(),
        GITHUB_VECTOR_SIGNATURE[:-1],
        "sha1=01dc10d0c83e72ed246219cdd91669667fe2ca59",
    ],
)
def test_bad_signatures_rejected(signature: str | None) -> None:
    headers = {} if signature is None else {"X-Hub-Signature-256": signature}
    assert not verify_signature(GITHUB_VECTOR_SECRET, headers, GITHUB_VECTOR_PAYLOAD)


def test_signature_covers_the_raw_bytes() -> None:
    assert not verify_signature(
        GITHUB_VECTOR_SECRET, {"X-Hub-Signature-256": GITHUB_VECTOR_SIGNATURE}, b"Hello, World! "
    )


def test_no_secret_never_verifies() -> None:
    signature = sign_body("", b"{}")
    assert not verify_signature("", {"X-Hub-Signature-256": signature}, b"{}")
    assert not verify_signature(None, {"X-Hub-Signature-256": signature}, b"{}")


def test_secret_policy() -> None:
    assert webhook_secret(Settings.for_tests()) == (None, "unset")
    assert webhook_secret(Settings.for_tests(github_webhook_secret=TEST_WEBHOOK_SECRET)) == (
        TEST_WEBHOOK_SECRET,
        None,
    )
    dev = Settings.for_tests(env="dev", github_webhook_secret=TEST_WEBHOOK_SECRET)
    assert webhook_secret(dev) == (None, "test_secret_outside_test")
    real = Settings.for_tests(env="prod", github_webhook_secret=SecretStr("a-real-secret-0123456789"))
    assert webhook_secret(real) == ("a-real-secret-0123456789", None)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_fixture_is_signed_with_the_test_secret(name: str) -> None:
    headers, body = load_fixture(name)
    assert verify_signature(TEST_WEBHOOK_SECRET, headers, body)
    assert headers["X-GitHub-Event"] in ALLOWED_EVENTS


def _load_make_fixtures() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_fixtures", ROOT / "scripts" / "make_fixtures.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixtures_are_deterministic_and_current() -> None:
    make_fixtures = _load_make_fixtures()
    first = make_fixtures.build_all()
    assert first == make_fixtures.build_all()
    for name, text in first.items():
        assert (ROOT / "fixtures" / "webhooks" / name).read_text(encoding="utf-8") == text, name


def test_fixture_shas_come_from_the_seed_constants() -> None:
    make_fixtures = _load_make_fixtures()
    shas = make_fixtures._seed_test_constants()
    pr = parse_github_event(*load_fixture("pr_opened"))
    assert pr.head_sha == shas["EXPECTED_BRANCHES"]["pr-1-partial-refunds"]
    assert pr.base_sha == shas["EXPECTED_BRANCHES"]["main"]


def test_parse_pr_opened() -> None:
    event = parse_github_event(*load_fixture("pr_opened"))
    assert event.kind == "pr_opened"
    assert event.delivery_id and event.event_name == "pull_request" and event.action == "opened"
    assert event.repo is not None
    assert (event.repo.external_id, event.repo.full_name, event.repo.owner_id) == (
        DEMO_REPO_ID,
        "coverset-demo/ledger",
        DEMO_OWNER_ID,
    )
    assert event.repo.private is False and event.repo.default_branch == "main"
    assert event.installation_id == 55 and event.repo.installation_id == 55
    pull = event.pull
    assert isinstance(pull, PullRef)
    assert (pull.number, pull.title, pull.author_association) == (1, "Support partial refunds", "MEMBER")
    assert pull.trusted and not pull.from_fork and not pull.draft
    assert pull.head_ref == "pr-1-partial-refunds"
    assert event.payload is not None and "clone_url" in event.payload["repository"]


def test_parse_pr_synchronize() -> None:
    event = parse_github_event(*load_fixture("pr_synchronize"))
    assert event.kind == "pr_sync" and event.pull is not None and event.pull.number == 2


def test_parse_fork_pr_is_untrusted() -> None:
    event = parse_github_event(*load_fixture("pr_opened_fork"))
    assert event.kind == "pr_opened" and event.pull is not None
    assert event.from_fork and event.pull.from_fork
    assert event.author_association == "NONE"
    assert not event.pull.trusted


def test_parse_push_main() -> None:
    event = parse_github_event(*load_fixture("push_main"))
    assert event.kind == "push" and event.ref == "refs/heads/main"
    assert event.head_sha and event.base_sha and event.head_sha != event.base_sha
    assert event.head_commit_message == "Main commit 6"


def test_parse_check_run_events() -> None:
    rerun = parse_github_event(*load_fixture("check_run_rerequested"))
    assert rerun.kind == "rerun" and rerun.check_run_id == 900_001 and rerun.check_external_id is None
    button = parse_github_event(*load_fixture("check_run_requested_action"))
    assert button.kind == "action_button" and button.action_identifier == "approve-run"
    assert button.sender == "dana-maintainer"


def test_parse_installation_and_ping() -> None:
    install = parse_github_event(*load_fixture("installation_created"))
    assert install.kind == "install" and install.installation_id == 55
    assert [(r.external_id, r.full_name, r.owner_id) for r in install.repos_added] == [
        (DEMO_REPO_ID, "coverset-demo/ledger", DEMO_OWNER_ID)
    ]
    ping = parse_github_event(*load_fixture("ping"))
    assert ping.kind == "ignore" and ping.reason == "ping"


def _mutated(name: str, mutate) -> tuple[dict[str, str], bytes]:  # type: ignore[no-untyped-def]
    headers, body = load_fixture(name)
    payload = json.loads(body)
    mutate(payload)
    return headers, json.dumps(payload).encode()


def test_push_to_other_branch_ignored() -> None:
    event = parse_github_event(*_mutated("push_main", lambda p: p.update(ref="refs/heads/feature")))
    assert event.kind == "ignore" and "not the default branch" in (event.reason or "")


def test_branch_creation_push_has_no_base() -> None:
    event = parse_github_event(*_mutated("push_main", lambda p: p.update(before="0" * 40, created=True)))
    assert event.kind == "push" and event.base_sha is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["pull_request"]["head"].update(sha="HEAD"),
        lambda p: p["pull_request"]["head"].update(sha="a" * 39),
        lambda p: p["pull_request"]["head"].update(sha="A" * 40),
        lambda p: p["repository"].update(full_name="../../etc"),
        lambda p: p["repository"].update(id="424242"),
        lambda p: p.pop("pull_request"),
    ],
    ids=["ref-name", "short", "uppercase", "path-name", "string-id", "missing-pr"],
)
def test_malformed_pull_requests_raise(mutate) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ForgeEventError):
        parse_github_event(*_mutated("pr_opened", mutate))


def test_pull_request_closed_and_draft() -> None:
    closed = parse_github_event(*_mutated("pr_opened", lambda p: p.update(action="closed")))
    assert closed.kind == "ignore"
    draft = parse_github_event(*_mutated("pr_opened", lambda p: p["pull_request"].update(draft=True)))
    assert draft.kind == "pr_opened" and draft.draft


def test_deleted_head_repo_counts_as_fork() -> None:
    event = parse_github_event(*_mutated("pr_opened", lambda p: p["pull_request"]["head"].update(repo=None)))
    assert event.from_fork


def test_check_suite_requested_is_ignored_but_rerequested_reruns() -> None:
    headers, body = load_fixture("check_run_rerequested")
    payload = json.loads(body)
    suite = {
        "action": "requested",
        "check_suite": {"id": 1, "head_sha": "c" * 40},
        "repository": payload["repository"],
    }
    headers = {**headers, "X-GitHub-Event": "check_suite"}
    assert parse_github_event(headers, json.dumps(suite).encode()).kind == "ignore"
    suite["action"] = "rerequested"
    rerun = parse_github_event(headers, json.dumps(suite).encode())
    assert rerun.kind == "rerun" and rerun.head_sha == "c" * 40


def test_unknown_event_and_bad_delivery() -> None:
    headers, body = load_fixture("ping")
    event = parse_github_event({**headers, "X-GitHub-Event": "issues"}, body)
    assert event.kind == "ignore" and "issues" in (event.reason or "")
    with pytest.raises(ForgeEventError):
        parse_github_event({**headers, "X-GitHub-Delivery": "../x"}, body)
    with pytest.raises(ForgeEventError):
        parse_github_event(headers, b"[1, 2]")


def test_mirror_credentials_never_expose_the_token() -> None:
    credentials = MirrorCredentials("ghs_" + "x" * 40)
    assert "ghs_" not in repr(credentials)
    args = credentials.git_config_args()
    assert args[0] == "-c" and args[1].startswith("http.extraHeader=Authorization: Basic ")
    assert "ghs_" not in args[1] and "x-access-token" not in args[1]


def test_deeply_nested_json_is_a_payload_error() -> None:
    with pytest.raises(ForgeEventError, match="nested too deeply"):
        decode_payload(b"[" * 100_000 + b"]" * 100_000)


def test_approval_fixture_names_the_awaiting_run() -> None:
    headers, body = load_fixture("check_run_requested_action")
    event = parse_github_event(headers, body)
    assert event.kind == "action_button" and event.action_identifier == "approve-run"
    assert event.check_external_id is not None and event.check_external_id.startswith("run_")


def test_repository_events_are_allowed_and_parsed() -> None:
    assert "repository" in ALLOWED_EVENTS
    headers, body = load_fixture("pr_opened")
    payload = json.loads(body)
    payload["action"] = "publicized"
    payload["repository"]["private"] = True  # the action decides, not a stale flag
    headers["X-GitHub-Event"] = "repository"
    event = parse_github_event(headers, json.dumps(payload).encode())
    assert event.kind == "repo_visibility" and event.repo is not None and event.repo.private is False


def test_composite_action_requires_a_pinned_package() -> None:
    yaml = pytest.importorskip("yaml")
    action = yaml.safe_load((ROOT / "src" / "coverset" / "forge" / "gha" / "action.yml").read_text())
    package = action["inputs"]["package"]
    assert package["required"] is True and "default" not in package
    install, wait = action["runs"]["steps"]
    assert "COVERSET_CI_PACKAGE" in install["run"] and 'if [ -z "${COVERSET_CI_PACKAGE}" ]' in install["run"]
    assert "env -u ACTIONS_ID_TOKEN_REQUEST_URL -u ACTIONS_ID_TOKEN_REQUEST_TOKEN" in install["run"]
    assert "ACTIONS_ID_TOKEN_REQUEST" not in json.dumps(install.get("env", {}))
    assert "::add-mask::" in wait["run"] and "COVERSET_OIDC_TOKEN" in wait["run"]
