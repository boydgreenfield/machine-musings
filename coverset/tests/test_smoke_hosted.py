"""``scripts/smoke_hosted.py`` against the local backends (a database of each test's own on the test
server, and blobs as files).

The pieces are tested one by one (the children's environment, the settings they share, the
report, the step bookkeeping, the database and store helpers), and one slow test runs the whole
script end to end on a temporary data directory, then cleans up after itself. Against Postgres and
a bucket the same script is run by hand (docs/deploy.md, "Smoke test a hosted state").
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from coverset.config import Settings
from coverset.db.session import Database, init_db
from coverset.db.testing import PostgresTestDatabases

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADMIN = "smoke-test-admin-token-0123456789abcdef"


@pytest.fixture(scope="module")
def smoke() -> Iterator[ModuleType]:
    """The script, imported by path (``scripts/`` is not a package)."""
    path = PROJECT_ROOT / "scripts" / "smoke_hosted.py"
    spec = importlib.util.spec_from_file_location("smoke_hosted_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture
def database_url(postgres_databases: PostgresTestDatabases) -> str:
    """A database of this test's own, with no schema yet (the smoke's migrate step builds it)."""
    return postgres_databases.create(empty=True)


def _environ(database_url: str, **extra: str) -> dict[str, str]:
    """The process environment without coverset's settings (the data dir comes from --data-dir), on
    the test's database."""
    base = {key: value for key, value in os.environ.items() if not key.startswith("COVERSET_")}
    return {**base, "COVERSET_DATABASE_URL": database_url, **extra}


def _rows(out: str) -> dict[str, str]:
    return {
        line.split()[1]: line.split()[0] for line in out.splitlines() if line[:4] in ("PASS", "WARN", "FAIL")
    }


def test_children_never_reach_github_or_modal(smoke: ModuleType, tmp_path: Path) -> None:
    source = {
        "GITHUB_TOKEN": "ghp_x",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid",
        "COVERSET_EXECUTOR": "modal",
        "COVERSET_GITHUB_APP_ID": "7",
        "COVERSET_GITHUB_TOKEN": "t" * 40,
        "COVERSET_DATABASE_URL": "postgresql+asyncpg://u:p@db/x",
        "AWS_ACCESS_KEY_ID": "AKIA",
    }
    env = smoke.child_environment(source, port=8765, admin_token=ADMIN, data_dir=tmp_path)
    assert not any(key.startswith(("GITHUB_", "ACTIONS_")) for key in env)
    assert "COVERSET_GITHUB_APP_ID" not in env and "COVERSET_GITHUB_TOKEN" not in env
    assert (env["COVERSET_EXECUTOR"], env["COVERSET_FORGE"]) == ("local", "fake")
    assert env["COVERSET_PUBLIC_URL"] == "http://127.0.0.1:8765" and env["COVERSET_BIND"] == "127.0.0.1:8765"
    assert env["COVERSET_ADMIN_TOKEN"] == ADMIN and env["COVERSET_DATA_DIR"] == str(tmp_path)
    assert env["COVERSET_DATABASE_URL"] == source["COVERSET_DATABASE_URL"], "the state settings pass through"
    assert env["AWS_ACCESS_KEY_ID"] == "AKIA"


def test_settings_match_what_the_children_see(smoke: ModuleType, tmp_path: Path) -> None:
    """The smoke's own data dir and the cache inside it replace the caller's (never the laptop's)."""
    env = smoke.child_environment(
        {
            "COVERSET_DATABASE_URL": "postgres://u:p@db.internal/coverset",
            "COVERSET_BLOB_URL": "s3://bucket/prefix?region=eu-west-1",
            "COVERSET_CACHE_DIR": str(tmp_path / "cache"),
        },
        port=8766,
        admin_token=ADMIN,
        data_dir=tmp_path / "data",
    )
    settings = smoke.settings_for(env)
    assert settings.hosted
    assert settings.database_url == "postgresql+asyncpg://u:p@db.internal/coverset"
    assert settings.data_dir == tmp_path / "data"
    assert settings.cache_dir == tmp_path / "data" / "cache", (
        "a COVERSET_CACHE_DIR from the caller is not used"
    )
    assert (settings.executor, settings.forge, settings.bind) == ("local", "fake", "127.0.0.1:8766")
    row = smoke.describe_state(settings)
    assert row.mark == "PASS" and "u:***@db.internal" in row.detail and ":p@" not in row.detail
    assert "s3://bucket/prefix;" in row.detail + ";" and "region" not in row.detail


def test_local_backends_are_a_warning_not_a_failure(smoke: ModuleType, tmp_path: Path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path)  # describes the settings, connects to nothing
    row = smoke.describe_state(settings)
    assert (row.mark, row.ok) == ("WARN", True)
    assert row.detail.startswith("not hosted (local backends)")


def _smoke(smoke: ModuleType, tmp_path: Path, database_url: str) -> Any:
    settings = Settings.for_tests(data_dir=tmp_path / "data", database_url=database_url)
    return smoke.Smoke(settings, {}, tmp_path, ADMIN, echo=lambda _line: None)


def test_steps_record_rows_and_stop_dependants(smoke: ModuleType, tmp_path: Path) -> None:
    run = _smoke(smoke, tmp_path, "postgresql+asyncpg://localhost:1/unused")  # no step connects
    with run.step("fine") as row:
        row["detail"] = "all good"
    with pytest.raises(smoke.StepFailed), run.step("refused") as row:
        row["ok"] = False
        row["detail"] = "HTTP 403"
    with pytest.raises(smoke.StepFailed), run.step("crashed"):
        raise RuntimeError("x" * 1000)
    marks = [(step.name, step.mark) for step in run.steps]
    assert marks == [("fine", "PASS"), ("refused", "FAIL"), ("crashed", "FAIL")]
    assert run.steps[2].detail.startswith("RuntimeError: xxx") and len(run.steps[2].detail) <= 300
    assert run.passed is False
    table = smoke.format_table(run.steps)
    assert table.splitlines()[0].startswith("PASS  fine")
    assert table.endswith("1 of 3 passed, 2 FAILED")


def test_database_helpers(smoke: ModuleType, tmp_path: Path, database_url: str) -> None:
    from coverset.db.models import Repo
    from coverset.web import tokens

    settings = Settings.for_tests(data_dir=tmp_path / "data", database_url=database_url)
    asyncio.run(init_db(settings))
    assert smoke.find_repo_id(settings) is None

    async def add_repo() -> int:
        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:
                repo = Repo(forge="github", external_id="424242", full_name=smoke.DEMO_REPO)
                session.add(repo)
                await session.commit()
                return repo.id
        finally:
            await database.engine.dispose()

    repo_id = asyncio.run(add_repo())
    assert smoke.find_repo_id(settings) == repo_id
    raw = smoke.create_upload_token(settings, repo_id, "smoke-test")

    async def check(raw: str) -> Any:
        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:
                return await tokens.verify(session, raw, now=datetime.now(UTC))
        finally:
            await database.engine.dispose()

    row = asyncio.run(check(raw))
    assert row is not None and row.repo_id == repo_id and row.scopes == ["upload"]
    assert smoke.latest_upload_run(settings, repo_id, "0" * 40) is None
    assert smoke.run_rows(settings, "run_missing") == {"run": None}


def test_run_keys_lists_one_run_from_the_store(smoke: ModuleType, tmp_path: Path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "data")
    run_dir = settings.data_dir / "facts" / "repo=3" / "run=run_a"
    run_dir.mkdir(parents=True)
    for name in ("MANIFEST.json", "tests_dim.parquet", "file_cov.shard=0.attempt=1.parquet"):
        (run_dir / name).write_bytes(b"x")
    other = settings.data_dir / "facts" / "repo=3" / "run=run_b"
    other.mkdir(parents=True)
    (other / "tests_dim.parquet").write_bytes(b"x")
    assert smoke.run_keys(settings, 3, "run_a") == [
        "facts/repo=3/run=run_a/MANIFEST.json",
        "facts/repo=3/run=run_a/file_cov.shard=0.attempt=1.parquet",
        "facts/repo=3/run=run_a/tests_dim.parquet",
    ]
    assert smoke.run_keys(settings, 3, "run_c") == []


def test_clean_only_on_an_empty_database(
    smoke: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    argv = ["--clean-only", "--workdir", str(tmp_path / "work"), "--data-dir", str(tmp_path / "data")]
    code = smoke.main(argv, environ=_environ(database_url))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "WARN  state" in out and "PASS  clean" in out and "nothing to clean" in out
    assert not (tmp_path / "work").exists(), "the scratch directory goes with --clean-only"
    assert not (tmp_path / "data").exists(), "--clean-only creates no data dir"


def _laptop(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    """A laptop's ``data/`` from ``make dev``: the demo's git repository and a key file."""
    data = tmp_path / "data"
    (data / "demo.git" / "refs").mkdir(parents=True)
    files = {
        "demo.git/HEAD": b"ref: refs/heads/main\n",
        "demo.git.shas.json": b"{}",
        "secret_key": b"k" * 64,
    }
    for name, content in files.items():
        (data / name).write_bytes(content)
    return data, files


def test_the_default_data_dir_is_the_smokes_own(
    smoke: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    """Run from a checkout with a laptop demo in data/: --clean-only leaves every byte of it alone."""
    monkeypatch.chdir(tmp_path)
    data, files = _laptop(tmp_path)
    code = smoke.main(["--clean-only", "--workdir", str(tmp_path / "work")], environ=_environ(database_url))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "nothing to clean" in out and str(tmp_path / "data" / "smoke-hosted") in out
    for name, content in files.items():
        assert (data / name).read_bytes() == content, name
    assert sorted(path.name for path in data.iterdir()) == ["demo.git", "demo.git.shas.json", "secret_key"]


def test_default_data_dirs_are_fixed_per_target(smoke: ModuleType) -> None:
    local = {"database": "postgresql+asyncpg://localhost/coverset", "store": "files in the data dir"}
    hosted = {"database": "postgresql+asyncpg://u:***@db/x", "store": "s3://b"}
    assert smoke.default_data_dir(local) == smoke.default_data_dir(dict(local))
    assert smoke.default_data_dir(local) != smoke.default_data_dir(hosted)
    assert smoke.default_data_dir(hosted).parent == Path("data") / "smoke-hosted"


def test_a_data_dir_with_foreign_files_is_refused(
    smoke: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    data, files = _laptop(tmp_path)
    argv = ["--workdir", str(tmp_path / "work"), "--data-dir", str(data)]
    code = smoke.main(argv, environ=_environ(database_url))
    out = capsys.readouterr().out
    assert code == 1
    assert _rows(out) == {"state": "WARN", "data": "FAIL"}, "nothing runs after the refusal"
    assert "holds files the smoke did not make" in out
    for name, content in files.items():
        assert (data / name).read_bytes() == content, name
    code = smoke.main([*argv, "--clean-only"], environ=_environ(database_url))
    out = capsys.readouterr().out
    assert code == 0 and "nothing to clean" in out
    assert (data / "demo.git" / "HEAD").exists()


def _database_with_demo_repo(smoke: ModuleType, path: Path, url: str) -> str:
    """``url``'s database, migrated and already holding the demo repository (as another data dir's)."""
    from coverset.db.models import Repo

    settings = Settings.for_tests(data_dir=path.parent / "elsewhere", database_url=url)
    asyncio.run(init_db(settings))

    async def add() -> None:
        database = Database.from_url(url)
        try:
            async with database.session() as session:
                session.add(
                    Repo(
                        forge="github",
                        external_id="1",
                        full_name=smoke.DEMO_REPO,
                        settings_json={"source": "x"},
                    )
                )
                await session.commit()
        finally:
            await database.engine.dispose()

    asyncio.run(add())
    return url


def test_a_demo_repository_the_smoke_did_not_make_is_never_touched(
    smoke: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    """``make dev-hosted``'s demo in the same database: the smoke refuses, and --clean keeps it."""
    monkeypatch.chdir(tmp_path)
    url = _database_with_demo_repo(smoke, tmp_path / "shared", database_url)
    environ = _environ(url)
    argv = ["--workdir", str(tmp_path / "work"), "--data-dir", str(tmp_path / "smoke")]
    code = smoke.main(argv, environ=environ)
    out = capsys.readouterr().out
    assert code == 1 and _rows(out)["data"] == "FAIL"
    assert "already holds coverset-demo/ledger" in out
    assert not (tmp_path / "smoke").exists()
    code = smoke.main([*argv, "--clean-only"], environ=environ)
    out = capsys.readouterr().out
    assert code == 0 and "nothing to clean" in out
    settings = Settings.for_tests(data_dir=tmp_path / "elsewhere", database_url=url)
    assert smoke.find_repo_id(settings) is not None, "the other demo repository is still there"


def test_a_marker_for_another_target_is_refused(smoke: ModuleType, tmp_path: Path, database_url: str) -> None:
    run = _smoke(smoke, tmp_path, database_url)
    run.target = smoke.target_of(run.settings)
    data = run.settings.data_dir
    data.mkdir(parents=True)
    (data / smoke.MARKER).write_text('{"target": {"database": "postgresql+asyncpg://x", "store": "s3://y"}}')
    with pytest.raises(smoke.StepFailed):
        smoke.step_data_dir(run)
    with pytest.raises(smoke.StepFailed):
        smoke.clean(run)
    assert [step.mark for step in run.steps] == ["FAIL", "FAIL"]
    assert "belongs to a smoke of" in run.steps[0].detail
    assert (data / smoke.MARKER).exists()


def test_the_data_dir_step_claims_and_reuses(smoke: ModuleType, tmp_path: Path, database_url: str) -> None:
    run = _smoke(smoke, tmp_path, database_url)
    run.target = smoke.target_of(run.settings)
    assert run.target["store"] == "files in the data dir"
    assert run.target["database"].startswith("postgresql+asyncpg://") and run.target["database"].endswith(
        database_url.rsplit("/", 1)[1]
    )
    smoke.step_data_dir(run)
    marker = smoke.read_marker(run.settings.data_dir)
    assert marker is not None and marker["target"] == run.target
    (run.settings.data_dir / "demo.git").mkdir()
    smoke.step_data_dir(run)
    assert [step.mark for step in run.steps] == ["PASS", "PASS"]
    assert "the smoke's own" in run.steps[1].detail


def test_free_port_is_bindable(smoke: ModuleType) -> None:
    import socket

    port = smoke.free_port()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_whole_smoke_on_local_backends(
    smoke: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    """Migrate, the demo, serve, four pages, an upload, its keys and rows; then --clean-only."""
    monkeypatch.chdir(tmp_path)  # no stray .env for the children
    environ = _environ(database_url)
    argv = ["--workdir", str(tmp_path / "work"), "--data-dir", str(tmp_path / "data")]
    code = smoke.main(argv, environ=environ)
    out = capsys.readouterr().out
    assert code == 0, out
    rows = _rows(out)
    assert rows.pop("state") == "WARN"
    assert set(rows.values()) == {"PASS"}, out
    assert {"data", "migrate", "demo", "serve", "upload", "bucket", "rows"} <= set(rows)
    assert ADMIN not in out
    assert (tmp_path / "data" / "demo.git").is_dir(), "the data stays in place"
    assert (tmp_path / "data" / smoke.MARKER).is_file()

    code = smoke.main([*argv, "--clean-only"], environ=environ)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "purged coverset-demo/ledger" in out
    assert not (tmp_path / "data").exists(), "the smoke's whole data dir goes"


def test_a_reset_database_clears_the_stale_cache(
    smoke: ModuleType, tmp_path: Path, database_url: str
) -> None:
    """After ``make services-reset`` the old cache's repo=1 would shadow the new database's repo 1."""
    run = _smoke(smoke, tmp_path, database_url)
    run.target = smoke.target_of(run.settings)
    smoke.step_data_dir(run)
    data = run.settings.data_dir
    smoke.write_marker(data, {**smoke.read_marker(data), "repo_id": 1})
    stale = data / "cache" / "facts" / "repo=1" / "dictionary.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    (data / "demo.git").mkdir()
    smoke.step_data_dir(run)  # the database (empty: no schema yet) holds no repository
    assert sorted(path.name for path in data.iterdir()) == [smoke.MARKER]
    assert "repo_id" not in smoke.read_marker(data)
    assert "old cache and git repository went" in run.steps[-1].detail


def test_a_demo_repository_with_another_id_is_not_the_smokes(
    smoke: ModuleType, tmp_path: Path, database_url: str
) -> None:
    url = _database_with_demo_repo(smoke, tmp_path / "shared", database_url)
    settings = Settings.for_tests(data_dir=tmp_path / "data", database_url=url)
    run = smoke.Smoke(settings, {}, tmp_path, ADMIN, echo=lambda _line: None)
    run.target = smoke.target_of(settings)
    settings.data_dir.mkdir()
    smoke.write_marker(settings.data_dir, {"target": run.target, "repo_id": 99})
    with pytest.raises(smoke.StepFailed):
        smoke.step_data_dir(run)
    assert "someone else's" in run.steps[-1].detail
    smoke.clean(run)
    assert "left coverset-demo/ledger (repo 1) alone" in run.steps[-1].detail
    assert smoke.find_repo_id(settings) == 1
    assert not settings.data_dir.exists()
