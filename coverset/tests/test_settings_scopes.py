"""Scoped settings: the catalogue contract, validation, precedence, the cache, the policy fallback,
and the orchestrator reading repository policy (intake, planner, verdict)."""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from coverset import settings_scopes as ss
from coverset.config import Settings
from coverset.db.models import AuditEvent, Repo, Run, ScopedSetting
from coverset.db.session import Database, init_db
from coverset.forge.github_events import parse_github_event
from coverset.lang.python import CoversetConfig
from coverset.orchestrator.intake import intake
from coverset.settings_scopes import (
    CATALOGUE,
    Policy,
    SettingError,
    apply_changes,
    clear_value,
    effective_policy,
    resolve,
    resolve_all,
    set_value,
    view_prefs,
)

ROOT = Path(__file__).resolve().parents[1]
WEBHOOKS = ROOT / "fixtures" / "webhooks"
DOC = ROOT / "docs" / "settings.md"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    ss.clear_cache()


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    await init_db(settings)
    database = Database.from_url(settings.database_url)
    yield database
    await database.engine.dispose()


async def _repo(db: Database, full_name: str = "Acme/widgets", *, ai_column: bool = False) -> Repo:
    async with db.session() as session:
        repo = Repo(forge="github", external_id=full_name, full_name=full_name, ai_review_enabled=ai_column)
        session.add(repo)
        await session.commit()
        await session.refresh(repo)
        return repo


async def _set(db: Database, key: str, value: object, scope: ss.ScopeKind, scope_id: object = None) -> None:
    async with db.session() as session:
        await set_value(session, key, value, scope=scope, scope_id=scope_id)  # type: ignore[arg-type]


# -- the catalogue ------------------------------------------------------------------------------


def test_catalogue_keeps_the_agreed_names_and_defaults() -> None:
    """Other features read these exact names; renaming one silently breaks them."""
    assert {k: s.default for k, s in CATALOGUE.items()} == {
        "view.highlight": "all",
        "view.map_mode": "gap",
        "view.map_module_weight": "thin",
        "view.theme": "auto",
        "policy.untrusted_pr_policy": "approve",
        "policy.run_drafts": False,
        "policy.ai_review": False,
        "policy.fail_fast": None,
        "policy.max_shards": None,
        "policy.target_shard_seconds": None,
        "policy.test_tax_threshold": 8.0,
        "policy.public_report": False,
    }
    assert all(s.user_overridable for k, s in CATALOGUE.items() if k.startswith("view."))
    assert not any(s.user_overridable for k, s in CATALOGUE.items() if k.startswith("policy."))
    assert CATALOGUE["view.highlight"].choices == ("all", "missing")
    assert CATALOGUE["view.map_mode"].choices == ("gap", "lines", "inputs")
    assert CATALOGUE["view.map_module_weight"].choices == ("full", "thin", "hidden")
    assert CATALOGUE["policy.fail_fast"].choices == ("finish_in_background", "cancel_remaining")
    assert (CATALOGUE["policy.max_shards"].minimum, CATALOGUE["policy.max_shards"].maximum) == (1, 128)


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("view.highlight", " missing ", "missing"),
        ("policy.run_drafts", "TRUE", True),
        ("policy.run_drafts", "off", False),
        ("policy.run_drafts", True, True),
        ("policy.max_shards", "8", 8),
        ("policy.max_shards", 128, 128),
        ("policy.test_tax_threshold", "2.5", 2.5),
        ("policy.test_tax_threshold", 3, 3.0),
    ],
)
def test_parse_accepts(key: str, raw: object, expected: object) -> None:
    value = CATALOGUE[key].parse(raw)
    assert value == expected and type(value) is type(expected)


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("view.highlight", "some"),
        ("view.theme", None),
        ("policy.run_drafts", "maybe"),
        ("policy.run_drafts", 1),
        ("policy.max_shards", "0"),
        ("policy.max_shards", 129),
        ("policy.max_shards", "4.5"),
        ("policy.max_shards", True),
        ("policy.max_shards", "１２"),  # full-width digits are not ASCII
        ("policy.test_tax_threshold", "nan"),
        ("policy.test_tax_threshold", "inf"),
        ("policy.test_tax_threshold", 0.1),
    ],
)
def test_parse_rejects(key: str, raw: object) -> None:
    with pytest.raises(SettingError):
        CATALOGUE[key].parse(raw)


def test_scope_ids_are_normalised_and_checked() -> None:
    assert ss.normalize_scope_id("org", " Acme ") == "acme"
    assert ss.normalize_scope_id("repo", 7) == "7"
    assert ss.normalize_scope_id("user", "local") == "local"
    assert ss.normalize_scope_id("instance", "ignored") == ""
    for scope, bad in (("org", "a/b"), ("org", ""), ("repo", "acme/x"), ("user", "@someone")):
        with pytest.raises(SettingError):
            ss.normalize_scope_id(scope, bad)  # type: ignore[arg-type]


def test_docs_table_matches_the_catalogue() -> None:
    """docs/settings.md carries the generated table verbatim (``coverset settings keys --markdown``)."""
    text = DOC.read_text(encoding="utf-8")
    start, end = "<!-- settings-keys:start -->\n", "\n<!-- settings-keys:end -->"
    assert start in text and end in text
    assert text.split(start, 1)[1].split(end, 1)[0] == ss.catalogue_markdown()


# -- precedence -----------------------------------------------------------------------------------


async def test_view_precedence_user_repo_org_instance_default(db: Database, settings: Settings) -> None:
    repo = await _repo(db)

    async def highlight(user: object = 5) -> Any:
        async with db.session() as session:
            return await resolve(session, "view.highlight", repo=repo, user_id=user, settings=settings)  # type: ignore[arg-type]

    assert await highlight() == "all"
    await _set(db, "view.highlight", "missing", "instance")
    assert await highlight() == "missing"
    await _set(db, "view.highlight", "all", "org", "ACME")  # owner names are case-insensitive
    assert await highlight() == "all"
    await _set(db, "view.highlight", "missing", "repo", repo.id)
    assert await highlight() == "missing"
    await _set(db, "view.highlight", "all", "user", 5)
    assert await highlight() == "all"
    assert await highlight(user=6) == "missing", "another user sees the repository's value"
    async with db.session() as session:
        prefs = await view_prefs(session, repo=repo, user_id=5, settings=settings)
    assert prefs == {
        "view.highlight": "all",
        "view.map_mode": "gap",
        "view.map_module_weight": "thin",
        "view.theme": "auto",
    }
    assert prefs.highlight == "all" and prefs.map_module_weight == "thin"


async def test_policy_precedence_repo_org_instance_config(db: Database, settings: Settings) -> None:
    repo = await _repo(db)
    other = await _repo(db, "Elsewhere/thing")

    async def policy(target: Repo = repo) -> Policy:
        async with db.session() as session:
            return await effective_policy(session, target, settings)

    assert (await policy()).untrusted_pr_policy == "approve"
    assert (await policy()).sources["policy.untrusted_pr_policy"] == "config"
    await _set(db, "policy.untrusted_pr_policy", "skip", "instance")
    assert (await policy()).untrusted_pr_policy == "skip"
    await _set(db, "policy.untrusted_pr_policy", "run_blocked_network", "org", "acme")
    assert (await policy()).untrusted_pr_policy == "run_blocked_network"
    await _set(db, "policy.untrusted_pr_policy", "approve", "repo", repo.id)
    current = await policy()
    assert (
        current.untrusted_pr_policy == "approve" and current.sources["policy.untrusted_pr_policy"] == "repo"
    )
    assert (await policy(other)).untrusted_pr_policy == "skip", "the org row covers only its owner"


async def test_users_cannot_override_policy(db: Database) -> None:
    async with db.session() as session:
        with pytest.raises(SettingError, match="cannot be set at user scope"):
            await set_value(session, "policy.run_drafts", True, scope="user", scope_id=5)
        assert await session.scalar(select(func.count()).select_from(ScopedSetting)) == 0


async def test_policy_falls_back_to_instance_configuration(db: Database, settings: Settings) -> None:
    repo = await _repo(db)
    drafts = settings.model_copy(update={"run_drafts": True})
    async with db.session() as session:
        policy = await effective_policy(session, repo, drafts)
    assert policy.run_drafts is True and policy.sources["policy.run_drafts"] == "config"
    assert (policy.max_shards, policy.fail_fast, policy.target_shard_seconds) == (None, None, None)
    assert policy.test_tax_threshold == 8.0 and policy.public_report is False and policy.ai_review is False
    await _set(db, "policy.run_drafts", False, "instance")
    async with db.session() as session:
        assert (await effective_policy(session, repo, drafts)).run_drafts is False, "a stored row wins"


async def test_token_mode_fallback_is_run_blocked_network(db: Database, settings: Settings) -> None:
    repo = await _repo(db)
    token = settings.model_copy(update={"github_token": SecretStr("ghp_" + "x" * 36), "forge": "github"})
    async with db.session() as session:
        layers = await ss.load_layers(session, repo=repo, settings=token)
    resolved = layers.resolve("policy.untrusted_pr_policy")
    assert resolved.value == "run_blocked_network" and "token-mode default" in resolved.detail


async def test_ai_review_column_acts_as_an_implicit_repository_value(
    db: Database, settings: Settings
) -> None:
    opted_in = await _repo(db, ai_column=True)
    plain = await _repo(db, "Acme/plain")

    async def ai(repo: Repo) -> tuple[bool, str]:
        async with db.session() as session:
            policy = await effective_policy(session, repo, settings)
        return policy.ai_review, policy.sources["policy.ai_review"]

    assert await ai(opted_in) == (True, "repo-column")
    assert await ai(plain) == (False, "default")
    await _set(db, "policy.ai_review", False, "org", "acme")
    assert await ai(opted_in) == (True, "repo-column"), "an org default never undoes a repo opt-in"
    await _set(db, "policy.ai_review", False, "repo", opted_in.id)
    assert await ai(opted_in) == (False, "repo"), "an explicit repository row wins over the column"
    await _set(db, "policy.ai_review", True, "instance")
    assert await ai(plain) == (False, "org")


async def test_resolve_all_and_unknown_key(db: Database, settings: Settings) -> None:
    async with db.session() as session:
        values = await resolve_all(session, settings=settings)
        assert set(values) == set(CATALOGUE)
        with pytest.raises(SettingError, match="unknown setting"):
            await resolve(session, "view.nope", settings=settings)


async def test_broader_than_is_what_a_reset_would_leave(db: Database, settings: Settings) -> None:
    repo = await _repo(db)
    await _set(db, "policy.max_shards", 12, "org", "acme")
    await _set(db, "policy.max_shards", 4, "repo", repo.id)
    async with db.session() as session:
        layers = await ss.load_layers(session, repo=repo, settings=settings)
    assert layers.resolve("policy.max_shards").value == 4
    inherited = layers.resolve("policy.max_shards", broader_than="repo")
    assert (inherited.value, inherited.detail) == (12, "organization Acme")
    assert layers.resolve("policy.max_shards", broader_than="org").source == "deferred"


# -- writes, audit and cache --------------------------------------------------------------------


async def test_writes_are_audited_and_unchanged_values_are_not(db: Database) -> None:
    async with db.session() as session:
        await set_value(session, "view.theme", "dark", scope="org", scope_id="Acme")
        await set_value(session, "view.theme", "dark", scope="org", scope_id="acme")  # unchanged
        assert await clear_value(session, "view.theme", scope="org", scope_id="acme")
        assert not await clear_value(session, "view.theme", scope="org", scope_id="acme")
        events = list(await session.scalars(select(AuditEvent).order_by(AuditEvent.id)))
    assert [e.action for e in events] == ["setting_changed", "setting_changed"]
    assert events[0].details == {
        "op": "set",
        "scope": "org",
        "scope_id": "acme",
        "key": "view.theme",
        "old": None,
        "new": "dark",
    }
    assert events[1].details["op"] == "clear" and events[1].details["old"] == "dark"
    assert {e.actor_kind for e in events} == {"system"}


async def test_apply_changes_is_all_or_nothing(db: Database) -> None:
    async with db.session() as session:
        with pytest.raises(SettingError):
            await apply_changes(session, {"view.theme": "dark", "policy.max_shards": "999"}, scope="instance")
        assert await session.scalar(select(func.count()).select_from(ScopedSetting)) == 0
        written = await apply_changes(
            session, {"view.theme": "dark", "policy.max_shards": "9"}, scope="instance"
        )
    assert written == {"view.theme": "dark", "policy.max_shards": 9}


async def test_cache_is_invalidated_on_write_and_expires(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(ss._CACHE, "_clock", lambda: clock[0])
    queries: list[str] = []
    real = ss.load_scopes

    async def counting(session: Any, scopes: Any, **kwargs: Any) -> Any:
        scopes = list(scopes)
        queries.extend(k for k in scopes if ss._CACHE.get(ss._engine(session), k) is None)
        return await real(session, scopes, **kwargs)

    monkeypatch.setattr(ss, "load_scopes", counting)

    async def theme() -> Any:
        async with db.session() as session:
            return await resolve(session, "view.theme", settings=settings)

    assert await theme() == "auto"
    assert await theme() == "auto"
    assert queries == [("instance", "")], "the second read is served from the cache"
    await _set(db, "view.theme", "dark", "instance")
    assert await theme() == "dark", "a write in this process invalidates at once"
    async with db.session() as session:  # another process's write: invisible until the TTL passes
        row = await session.scalar(select(ScopedSetting).where(ScopedSetting.key == "view.theme"))
        assert row is not None
        row.value = {"v": "light"}
        await session.commit()
    assert await theme() == "dark"
    clock[0] += ss.CACHE_TTL_S + 1
    assert await theme() == "light"


async def test_invalid_stored_rows_fall_through(db: Database, settings: Settings) -> None:
    async with db.session() as session:
        session.add(
            ScopedSetting(scope_kind="instance", scope_id="", key="view.theme", value={"v": "purple"})
        )
        session.add(ScopedSetting(scope_kind="instance", scope_id="", key="view.gone", value={"v": 1}))
        session.add(
            ScopedSetting(scope_kind="user", scope_id="3", key="policy.run_drafts", value={"v": True})
        )
        await session.commit()
        assert await resolve(session, "view.theme", settings=settings) == "auto"
        assert await resolve(session, "policy.run_drafts", user_id=3, settings=settings) is False


# -- the policy reaching the orchestrator -------------------------------------------------------


def test_policy_apply_replaces_fail_fast_and_target_and_caps_max_shards() -> None:
    base = Policy("approve", False, False, None, None, None, 8.0, False)
    config = CoversetConfig(max_shards=40, fail_fast="finish_in_background", target_shard_seconds=20.0)
    assert base.apply(config, instance_max_shards=32).max_shards == 32, "[tool.coverset] is capped too"
    assert base.apply(config, instance_max_shards=64) == config
    scoped = Policy("approve", False, False, "cancel_remaining", 100, 5, 8.0, False)
    applied = scoped.apply(config, instance_max_shards=32)
    assert (applied.max_shards, applied.fail_fast, applied.target_shard_seconds) == (
        32,
        "cancel_remaining",
        5.0,
    )
    assert scoped.apply(config, instance_max_shards=128).max_shards == 40, "a cap never raises the file"
    capped = Policy("approve", False, False, None, 8, None, 8.0, False)
    assert capped.apply(config, instance_max_shards=128).max_shards == 8, "a lower scoped cap wins"
    asks_for_two = CoversetConfig(max_shards=2)
    assert capped.apply(asks_for_two, instance_max_shards=128).max_shards == 2, "the file's 2 stays 2"


def _event(name: str, *, sha: str | None = None, number: int | None = None, draft: bool = False) -> Any:
    """A fixture webhook as a ForgeEvent, optionally with another head, number or draft flag."""
    data = json.loads((WEBHOOKS / f"{name}.json").read_text(encoding="utf-8"))
    payload = json.loads(data["body"])
    pull = payload["pull_request"]
    if sha is not None:
        pull["head"]["sha"] = sha
    if number is not None:
        pull["number"] = payload["number"] = number
    pull["draft"] = draft
    headers = dict(data["headers"])
    headers["X-GitHub-Delivery"] = str(uuid.uuid4())
    return parse_github_event(headers, json.dumps(payload).encode())


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    from coverset.forge.signing import TEST_WEBHOOK_SECRET

    return {"github_webhook_secret": TEST_WEBHOOK_SECRET, "allowed_owners": ["coverset-demo"]}


async def test_intake_reads_repository_policy_over_the_instance(db: Database, settings: Settings) -> None:
    async def run(event: Any) -> Any:
        async with db.session() as session:
            return await intake(session, settings, "github", event, received_at=settings.now())

    first = await run(_event("pr_opened"))
    assert first.status == "created"
    assert (await run(_event("pr_opened_fork"))).status == "awaiting_approval", "instance default: approve"
    await _set(db, "policy.untrusted_pr_policy", "skip", "instance")
    skipped = await run(_event("pr_opened_fork", sha="c" * 40))
    assert skipped.status == "ignored_event" and "skip" in (skipped.reason or "")
    await _set(db, "policy.untrusted_pr_policy", "run_blocked_network", "repo", first.repo_id)
    blocked = await run(_event("pr_opened_fork", sha="d" * 40))
    assert blocked.status == "created", "the repository row overrides the instance's skip"
    async with db.session() as session:
        row = await session.get(Run, blocked.run_id)
    assert row is not None and (row.trusted, row.network) == (False, "blocked")

    draft = _event("pr_opened", sha="e" * 40, number=7, draft=True)
    assert (await run(draft)).status == "ignored_event", "COVERSET_RUN_DRAFTS is off"
    await _set(db, "policy.run_drafts", True, "org", "coverset-demo")
    redo = _event("pr_opened", sha="f" * 40, number=8, draft=True)
    assert (await run(redo)).status == "created", "an org row turns drafts on"


async def test_verdict_ai_review_reads_the_scoped_policy(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.ai import client as ai_client
    from coverset.orchestrator import verdict

    repo = await _repo(db)
    seen: list[bool] = []

    def fake_client(_settings: Settings, *, repo_enabled: bool) -> None:
        seen.append(repo_enabled)

    change = SimpleNamespace(region=SimpleNamespace(kind="function", func_key="m.f"), prev=None)
    monkeypatch.setattr(ai_client, "get_review_client", fake_client)
    monkeypatch.setattr(verdict, "source_changes", lambda plan: [change])
    run = SimpleNamespace(repo_id=repo.id, trusted=True, head_sha="a" * 40, id="run_x")
    for flip in (True, False):
        async with db.session() as session:
            source, _ = await verdict._ai_review(session, settings, run, None, None)  # type: ignore[arg-type]
        assert source == "off"
        if flip:
            await _set(db, "policy.ai_review", True, "repo", repo.id)
    assert seen == [False, True]


# -- the planner (uses the orchestrator test fakes) ------------------------------------------------


@pytest.fixture
def orch() -> Any:
    sys.path.insert(0, str(Path(__file__).parent / "orchestrator"))
    import orch_fakes

    return orch_fakes


async def test_planner_applies_repository_policy(
    db: Database, settings: Settings, tmp_path: Path, orch: Any
) -> None:
    from coverset.facts.store import FactsStore
    from coverset.orchestrator.planner import plan_run
    from coverset.orchestrator.repos import register_local_repo

    source = orch.make_repo(tmp_path / "src-repo")
    async with db.session() as session:
        repo = await register_local_repo(session, source.path)
        await session.commit()
        repo_id = repo.id
        run = Run(
            repo_id=repo_id, head_sha=source.main_sha, trigger="baseline", status="running", trusted=True
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
    await _set(db, "policy.fail_fast", "cancel_remaining", "repo", repo_id)
    await _set(db, "policy.max_shards", 100, "repo", repo_id)
    await _set(db, "policy.target_shard_seconds", 7, "org", "local")
    capped = settings.model_copy(update={"max_shards": 3})
    executor = orch.FakeExecutor(orch.Script())
    plan = await plan_run(
        db,
        capped,
        FactsStore(settings.facts_dir),
        run,
        executor=executor,
        env_spec_builder=orch.fake_env_spec,
    )
    try:
        assert plan.config.fail_fast == "cancel_remaining", "the repository row beats [tool.coverset]"
        assert plan.config.max_shards == 3, "and COVERSET_MAX_SHARDS still caps it"
        assert plan.config.target_shard_seconds == 7.0, "the org row reaches a local repository too"
    finally:
        await plan.cleanup()


# -- scopes outliving what they name --------------------------------------------------------------


async def _purge(db: Database, repo_id: int) -> None:
    """What ``coverset repo purge`` does to the row (plus the scope cleanup it should call)."""
    async with db.session() as session:
        await session.execute(delete(Repo).where(Repo.id == repo_id))
        await session.commit()


@pytest.mark.sqlite_only  # Postgres sequences never hand out a deleted id again
async def test_orphaned_repo_rows_never_reach_the_next_repository_with_that_id(
    db: Database, settings: Settings
) -> None:
    """SQLite reuses the highest rowid: a purged repository's policy must not relax the next one's."""
    await _repo(db, "acme/first")
    gone = await _repo(db, "acme/public-new")
    await _set(db, "policy.untrusted_pr_policy", "run_blocked_network", "repo", gone.id)
    await _set(db, "policy.public_report", True, "repo", gone.id)
    async with db.session() as session:  # warm the cache, as a running serve would have
        assert (await effective_policy(session, gone, settings)).public_report is True
    await _purge(db, gone.id)  # without forget_scope: the rows stay behind
    newcomer = await _repo(db, "newco/unrelated")
    assert newcomer.id == gone.id, "the precondition: SQLite handed out the same id"
    async with db.session() as session:
        policy = await effective_policy(session, newcomer, settings)
        stub = await ss.repo_stub(session, newcomer.id)
        prefs = await view_prefs(session, repo=stub, user_id=None, settings=settings)
    assert policy.public_report is False and policy.sources["policy.public_report"] == "default"
    assert policy.untrusted_pr_policy == ss.fallback(CATALOGUE["policy.untrusted_pr_policy"], settings).value
    assert policy.sources["policy.untrusted_pr_policy"] == "config"
    assert prefs == ss.default_view_prefs()
    # Re-entering the same value at the new repository rewrites the orphan, so it takes effect.
    await _set(db, "policy.public_report", True, "repo", newcomer.id)
    async with db.session() as session:
        assert (await effective_policy(session, newcomer, settings)).public_report is True


async def test_forget_scope_deletes_the_rows_and_the_cache(db: Database, settings: Settings) -> None:
    repo = await _repo(db)
    await _set(db, "policy.run_drafts", True, "repo", repo.id)
    await _set(db, "view.theme", "dark", "repo", repo.id)
    await _set(db, "view.theme", "light", "user", 7)
    async with db.session() as session:
        assert (await effective_policy(session, repo, settings)).run_drafts is True
        assert await ss.forget_scope(session, "repo", repo.id, commit=True) == 2
        assert (await effective_policy(session, repo, settings)).run_drafts is False
        left = await session.scalar(select(func.count()).select_from(ScopedSetting))
    assert left == 1, "other scopes are untouched"
