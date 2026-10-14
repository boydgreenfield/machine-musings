"""The hosted shape (Postgres + an S3-compatible bucket) from the operator's side.

``coverset deploy check``'s database, object store, cache and state rows (with the two probes
stubbed: the probes themselves are tested with the database and storage layers); ``serve``'s
migration at start; ``coverset dev --hosted`` and ``--tunnel``; the Modal web module's choice
between the stateless hosted mode and the Volume mode, with ``modal`` stubbed out so nothing
reaches Modal; and the deployment files (compose, Fly, AWS, Dockerfile) parse and say what the
docs say they do.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner
from pydantic import SecretStr

from coverset.cli import cli, migrate_before_serving
from coverset.commands import deploy_cmd, dev
from coverset.commands.deploy_cmd import Check, deployment_checks
from coverset.config import Settings, get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = PROJECT_ROOT / "deploy"
PG_URL = "postgresql+asyncpg://coverset:pw@db.internal:5432/coverset"
BLOB_URL = "s3://coverset-prod/cs?region=us-east-1"
PUBLIC_URL = "https://coverset.example.com"


def _forget_after(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    """Unset ``keys`` now and after the test, also those the code under test sets itself.

    ``monkeypatch.delenv`` records nothing for a key that is absent, so a key the code later puts
    into ``os.environ`` would leak into the next test; setting it first records its absence.
    """
    for key in keys:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _rows(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


def _hosted(tmp_path: Path, admin_token: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "prod",
        "data_dir": tmp_path / "data",
        "public_url": PUBLIC_URL,
        "admin_token": SecretStr(admin_token),
        "database_url": PG_URL,
        "blob_url": BLOB_URL,
    }
    values.update(overrides)
    return Settings.for_tests(**values)


def _checks(settings: Settings, **kwargs: Any) -> dict[str, Check]:
    kwargs.setdefault("database_probe", lambda _url: ("pass", "Postgres 17.6, migrated to head 0009"))
    kwargs.setdefault("store_probe", lambda _settings: ("pass", "s3://coverset-prod/cs: ok (12 ms)"))
    return _rows(deployment_checks(settings, environ={}, prober=None, in_modal=False, **kwargs))


# -- coverset deploy check: the state rows ---------------------------------------------------------


def test_hosted_rows_come_from_the_two_probes(tmp_path: Path, admin_token: str) -> None:
    seen: dict[str, Any] = {}

    def database_probe(url: str) -> tuple[str, str]:
        seen["url"] = url
        return "pass", "Postgres 17.6, migrated to head 0009; pool 5 + 10 overflow per process"

    def store_probe(settings: Settings) -> tuple[str, str]:
        seen["blob"] = settings.blob_url
        return "warn", "s3://coverset-prod/cs: the store ignores If-Match"

    rows = _checks(_hosted(tmp_path, admin_token), database_probe=database_probe, store_probe=store_probe)
    assert seen == {"url": PG_URL, "blob": BLOB_URL}
    assert (rows["database"].status, rows["database"].detail) == (
        "pass",
        "Postgres 17.6, migrated to head 0009; pool 5 + 10 overflow per process",
    )
    assert rows["object store"].status == "warn" and "If-Match" in rows["object store"].detail
    assert rows["state"].status == "pass" and rows["state"].detail.startswith("hosted")
    assert str(tmp_path / "data") in rows["state"].detail
    names = [name for name in rows]
    assert (
        names.index("database")
        < names.index("object store")
        < names.index("cache dir")
        < names.index("state")
    )


def test_a_probe_that_raises_is_a_failing_row(tmp_path: Path, admin_token: str) -> None:
    def boom(_arg: Any) -> Any:
        raise OSError("connection refused")

    rows = _checks(_hosted(tmp_path, admin_token), database_probe=boom, store_probe=boom)
    assert rows["database"].status == "fail" and "connection refused" in rows["database"].detail
    assert rows["object store"].status == "fail"
    assert rows["object store"].detail.startswith("s3://coverset-prod/cs:"), "no query string in the row"


def test_async_probes_are_awaited(tmp_path: Path, admin_token: str) -> None:
    async def database_probe(_url: str) -> tuple[str, str]:
        return "warn", "Postgres 17.6, not migrated yet"

    rows = _checks(_hosted(tmp_path, admin_token), database_probe=database_probe)
    assert rows["database"].status == "warn"


def test_the_real_probes_answer_for_a_laptop(tmp_path: Path) -> None:
    """Without stubs: SQLite that does not exist yet warns (never created), local files pass."""
    settings = Settings.for_tests(data_dir=tmp_path / "data")
    rows = _rows(deployment_checks(settings, environ={}, prober=None, in_modal=False))
    assert rows["database"].status == "warn" and "does not exist yet" in rows["database"].detail
    assert not (tmp_path / "data" / "coverset.db").exists()
    assert rows["object store"].status == "pass" and "local files" in rows["object store"].detail
    assert rows["state"].status == "pass" and rows["state"].detail.startswith("laptop")


@pytest.mark.parametrize(
    ("overrides", "status", "words"),
    [
        ({"blob_url": ""}, "warn", "Postgres, but facts and uploads are files"),
        ({"database_url": ""}, "warn", "object store, but the database is SQLite"),
        ({"database_url": "", "blob_url": ""}, "warn", "behind the public https://coverset.example.com"),
    ],
)
def test_state_row_names_what_still_needs_a_disk(
    tmp_path: Path, admin_token: str, overrides: dict[str, Any], status: str, words: str
) -> None:
    rows = _checks(_hosted(tmp_path, admin_token, **overrides))
    assert rows["state"].status == status
    assert words in rows["state"].detail


def test_check_flags_skip_the_connecting_rows(tmp_path: Path, admin_token: str) -> None:
    def never(_arg: Any) -> Any:
        raise AssertionError("must not be called")

    rows = _checks(
        _hosted(tmp_path, admin_token),
        check_database=False,
        check_store=False,
        database_probe=never,
        store_probe=never,
    )
    assert "database" not in rows and "object store" not in rows
    assert "cache dir" in rows and "state" in rows


def test_cache_row_reports_size_and_bound(
    tmp_path: Path, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    (cache / "facts").mkdir(parents=True)
    (cache / "facts" / "a.parquet").write_bytes(b"x" * 4096)
    monkeypatch.setenv("COVERSET_CACHE_MAX_GB", "0.25")
    settings = _hosted(tmp_path, admin_token, cache_dir=cache)
    row = _checks(settings)["cache dir"]
    assert row.status == "pass", row.detail
    assert str(cache) in row.detail and "1 files" in row.detail and "bound 0.25 GB" in row.detail
    assert not list(cache.glob(".deploy-check-*")), "the write probe cleans up"


def test_cache_row_warns_when_the_bound_does_not_fit(
    tmp_path: Path, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERSET_CACHE_MAX_GB", "1000000")  # a petabyte
    row = _checks(_hosted(tmp_path, admin_token, cache_dir=tmp_path / "cache"))["cache dir"]
    assert row.status == "warn" and "COVERSET_CACHE_MAX_GB" in row.detail


def test_cache_row_fails_when_unwritable(tmp_path: Path, admin_token: str) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    row = _checks(_hosted(tmp_path, admin_token, cache_dir=blocker / "cache"))["cache dir"]
    assert row.status == "fail" and "not writable" in row.detail


def test_json_output_keeps_its_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, admin_token: str
) -> None:
    """One ``{"detail", "name", "status"}`` object per row, the new rows included."""
    monkeypatch.chdir(tmp_path)
    for key in [key for key in os.environ if key.startswith("COVERSET_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("COVERSET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COVERSET_DATABASE_URL", PG_URL)
    monkeypatch.setenv("COVERSET_BLOB_URL", BLOB_URL)
    monkeypatch.setattr(deploy_cmd, "_profile_has_token", lambda: False)
    import coverset.db.session
    import coverset.storage

    monkeypatch.setattr(
        coverset.db.session, "check_database", lambda _url: ("pass", "Postgres 17.6, at head")
    )
    monkeypatch.setattr(coverset.storage, "check_store", lambda _settings: ("pass", "bucket ok"))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["deploy", "check", "--offline", "--json"])
    finally:
        get_settings.cache_clear()
    rows = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert {tuple(sorted(row)) for row in rows} == {("detail", "name", "status")}
    by_name = {row["name"]: row for row in rows}
    assert by_name["database"]["detail"] == "Postgres 17.6, at head"
    assert by_name["object store"]["detail"] == "bucket ok"
    assert by_name["state"]["detail"].startswith("hosted")
    assert "cache dir" in by_name


# -- serve migrates before it listens -------------------------------------------------------------


def test_serve_migrates_first_and_the_app_does_not_again(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    served = migrate_before_serving(settings)
    assert served.skip_migrations is True, "the app's own startup migration is skipped"
    first = capsys.readouterr().out
    assert "database ready (" in first
    migrate_before_serving(settings)
    assert "already at the latest revision" in capsys.readouterr().out


def test_serve_without_migrate_on_start_migrates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "data", migrate_on_start=False)
    served = migrate_before_serving(settings)
    assert served.skip_migrations is True
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "data" / "coverset.db").exists()


def test_skip_migrations_keeps_its_meaning(tmp_path: Path) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "data", skip_migrations=True)
    assert migrate_before_serving(settings) is settings
    assert not (tmp_path / "data" / "coverset.db").exists()


def test_migrate_on_start_reads_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COVERSET_MIGRATE_ON_START", "false")
    assert Settings().migrate_on_start is False
    assert Settings.for_tests().migrate_on_start is True


def test_serve_command_uses_the_migrated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import coverset.app
    import coverset.cli

    built: list[Settings] = []
    monkeypatch.setattr(coverset.app, "create_app", lambda s: built.append(s) or object())
    monkeypatch.setattr(coverset.cli, "_serve", _no_serve)
    result = CliRunner().invoke(
        cli,
        ["serve", "--bind", "127.0.0.1:8131"],
        env={"COVERSET_DATA_DIR": str(tmp_path / "data"), "COVERSET_DATABASE_URL": ""},
    )
    assert result.exit_code == 0, result.output
    assert "database ready" in result.output
    assert len(built) == 1 and built[0].skip_migrations is True
    assert (tmp_path / "data" / "coverset.db").is_file()


async def _no_serve(_app: Any, _bind: str) -> None:
    return None


# -- coverset dev --hosted ---------------------------------------------------------------------------


class FakeDocker:
    """Answers the ``docker compose`` calls ``ensure_dev_stack`` makes."""

    def __init__(self, running: set[str], ports: dict[tuple[str, str], str] | None = None) -> None:
        self.running = running
        self.ports = ports or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        args = argv[4:]  # after: docker compose -f FILE
        if args[:1] == ["ps"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(sorted(self.running)) + "\n", "")
        if args[:1] == ["up"]:
            self.running |= {"postgres", "minio", "minio-init"}
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[:1] == ["port"]:
            published = self.ports.get((args[1], args[2]))
            return subprocess.CompletedProcess(argv, 0 if published else 1, (published or "") + "\n", "")
        raise AssertionError(argv)


def test_a_stopped_stack_is_started_and_its_ports_discovered() -> None:
    docker = FakeDocker(set(), {("postgres", "5432"): "127.0.0.1:55432", ("minio", "9000"): "127.0.0.1:9000"})
    lines: list[str] = []
    stack = dev.ensure_dev_stack(
        dev.DevStack(),
        echo=lines.append,
        run=docker,
        which=lambda _name: "/usr/bin/docker",
        probe=lambda _p: True,
    )
    assert any(call[4:7] == ["up", "-d", "--wait"] for call in docker.calls)
    assert stack == dev.DevStack(pg_port=55432, s3_port=9000, console_port=9001)
    assert dev.DevStack.from_environ({}) == stack, "the compose file's defaults"
    assert "starting the dev services" in lines[0]
    assert stack.database_url == "postgresql+asyncpg://coverset:coverset@127.0.0.1:55432/coverset"


def test_a_running_stack_is_left_alone() -> None:
    docker = FakeDocker({"postgres", "minio", "minio-init"})
    lines: list[str] = []
    dev.ensure_dev_stack(
        dev.DevStack(),
        echo=lines.append,
        run=docker,
        which=lambda _name: "/usr/bin/docker",
        probe=lambda _p: True,
    )
    assert not any(call[4:5] == ["up"] for call in docker.calls)
    assert "are running" in lines[0]


def test_without_docker_the_ports_decide() -> None:
    lines: list[str] = []
    with pytest.raises(click.ClickException, match="MinIO on 127.0.0.1:9000"):
        dev.ensure_dev_stack(
            dev.DevStack(),
            echo=lines.append,
            run=FakeDocker(set()),
            which=lambda _name: None,
            probe=lambda port: port == 55432,
        )
    assert "docker is not available" in lines[0]


def test_failed_compose_up_is_a_clean_error() -> None:
    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[4] == "ps":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 17, "", "")

    with pytest.raises(click.ClickException, match="exit 17"):
        dev.ensure_dev_stack(dev.DevStack(), echo=lambda _l: None, run=run, which=lambda _n: "docker")


def test_ports_follow_the_compose_variables() -> None:
    stack = dev.DevStack.from_environ({"COVERSET_DEV_PG_PORT": "15432", "COVERSET_DEV_S3_PORT": "19000"})
    assert (stack.pg_port, stack.s3_port, stack.console_port) == (15432, 19000, 9001)
    with pytest.raises(click.ClickException, match="not a TCP port"):
        dev.DevStack.from_environ({"COVERSET_DEV_PG_PORT": "postgres"})


def test_hosted_environment_points_at_the_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env: dict[str, str] = {"COVERSET_DATABASE_URL": "sqlite+aiosqlite:///elsewhere.db"}
    lines = dev.apply_hosted_environment(dev.DevStack(pg_port=55432), env)
    assert env["COVERSET_DATABASE_URL"] == "postgresql+asyncpg://coverset:coverset@127.0.0.1:55432/coverset"
    assert env["COVERSET_BLOB_URL"] == "s3://coverset?endpoint_url=http://127.0.0.1:9000&region=us-east-1"
    assert (env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"]) == ("minioadmin", "minioadmin")
    assert env["COVERSET_DATA_DIR"] == str(dev.DEV_HOSTED_DATA_DIR)
    assert any("MinIO console http://127.0.0.1:9001" in line for line in lines)
    kept = {"COVERSET_DATA_DIR": str(tmp_path)}
    dev.apply_hosted_environment(dev.DevStack(), kept)
    assert kept["COVERSET_DATA_DIR"] == str(tmp_path), "an explicit data dir is kept"
    monkeypatch.chdir(tmp_path)
    for key in [key for key in os.environ if key.startswith("COVERSET_")]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert Settings().hosted is True


def test_hosted_environment_drops_the_shells_aws_identity() -> None:
    """An SSO session token next to the MinIO keys makes MinIO refuse every request."""
    env = {
        "AWS_ACCESS_KEY_ID": "ASIAREAL",
        "AWS_SECRET_ACCESS_KEY": "real",
        "AWS_SESSION_TOKEN": "sso-token",
        "AWS_SECURITY_TOKEN": "sso-token",
        "AWS_PROFILE": "work",
        "AWS_DEFAULT_PROFILE": "work",
        "AWS_REGION": "eu-west-1",
    }
    lines = dev.apply_hosted_environment(dev.DevStack(), env)
    assert not set(dev.AWS_SHELL_IDENTITY) & set(env)
    assert (env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"]) == dev.DEV_S3_CREDENTIALS
    assert env["AWS_REGION"] == "eu-west-1", "only the identity goes"
    assert any("ignoring AWS_SESSION_TOKEN" in line for line in lines)
    clean: dict[str, str] = {}
    assert not any("ignoring" in line for line in dev.apply_hosted_environment(dev.DevStack(), clean))


def test_dev_hosted_wires_the_stack_into_the_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(dev, "ensure_dev_stack", lambda stack: dev.DevStack(pg_port=55432))
    monkeypatch.setattr(dev, "data_dir_has_repository", lambda _settings: True)
    monkeypatch.setattr(
        dev,
        "run_reloader",
        lambda bind, open_browser=False: started.append(os.environ["COVERSET_DATABASE_URL"]),
    )
    _forget_after(
        monkeypatch,
        "COVERSET_DATABASE_URL",
        "COVERSET_BLOB_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    )
    monkeypatch.setenv("COVERSET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["dev", "--hosted", "--bind", "127.0.0.1:8126"])
    assert result.exit_code == 0, result.output
    assert started == ["postgresql+asyncpg://coverset:coverset@127.0.0.1:55432/coverset"]
    assert "hosted-like: Postgres 127.0.0.1:55432/coverset" in result.output
    assert "warning" not in result.output


# -- coverset dev --tunnel ---------------------------------------------------------------------------


def test_tunnel_recipe_when_nothing_is_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    served: list[str] = []
    monkeypatch.setattr(dev, "run_reloader", lambda bind, open_browser=False: served.append(bind))
    monkeypatch.chdir(tmp_path)
    env = {"COVERSET_DATA_DIR": str(tmp_path / "data"), "COVERSET_PUBLIC_URL": "", "COVERSET_ADMIN_TOKEN": ""}
    result = CliRunner().invoke(cli, ["dev", "--tunnel", "--bind", "127.0.0.1:8127"], env=env)
    assert result.exit_code == 0, result.output
    assert served == [], "not ready: nothing is served"
    out = result.output
    assert "ngrok http http://127.0.0.1:8127" in out
    assert "cloudflared tunnel --url http://127.0.0.1:8127" in out
    assert "export COVERSET_PUBLIC_URL=https://<the tunnel's host>" in out
    token_line = next(line for line in out.splitlines() if "export COVERSET_ADMIN_TOKEN=" in line)
    token = token_line.split("=", 1)[1].split()[0]
    assert len(token) == 64, "a fresh 32-byte token, long enough for the 32-character rule"
    assert "not ready: COVERSET_PUBLIC_URL is http://127.0.0.1:8127" in out
    assert "COVERSET_ADMIN_TOKEN is unset" in out


def test_tunnel_with_a_public_url_but_no_token_explains_the_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dev, "run_reloader", lambda bind, open_browser=False: None)
    monkeypatch.chdir(tmp_path)
    env = {
        "COVERSET_DATA_DIR": str(tmp_path / "data"),
        "COVERSET_PUBLIC_URL": "https://quiet-otter.ngrok-free.app",
        "COVERSET_ADMIN_TOKEN": "",
    }
    result = CliRunner().invoke(cli, ["dev", "--tunnel"], env=env)
    assert result.exit_code == 0, result.output
    assert "not ready: invalid configuration" in result.output and "COVERSET_ADMIN_TOKEN" in result.output


def test_tunnel_ready_prints_the_command_and_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, admin_token: str
) -> None:
    served: list[str] = []
    monkeypatch.setattr(dev, "run_reloader", lambda bind, open_browser=False: served.append(bind))
    monkeypatch.setattr(dev, "seed_in_child", lambda: None)
    monkeypatch.chdir(tmp_path)
    env = {
        "COVERSET_DATA_DIR": str(tmp_path / "data"),
        "COVERSET_PUBLIC_URL": "https://quiet-otter.ngrok-free.app",
        "COVERSET_ADMIN_TOKEN": admin_token,
    }
    result = CliRunner().invoke(cli, ["dev", "--tunnel", "--no-seed"], env=env)
    assert result.exit_code == 0, result.output
    assert served == ["127.0.0.1:8000"]
    assert "ngrok http --url=https://quiet-otter.ngrok-free.app http://127.0.0.1:8000" in result.output
    assert "public demo on https://quiet-otter.ngrok-free.app" in result.output
    assert admin_token not in result.output


def test_tunnel_problems_is_a_pure_function(tmp_path: Path, admin_token: str) -> None:
    local = Settings.for_tests(data_dir=tmp_path)
    assert len(dev.tunnel_problems(local)) == 2
    plain = Settings.for_tests(
        data_dir=tmp_path, public_url="http://demo.example.org", admin_token=admin_token
    )
    assert dev.tunnel_problems(plain) == [
        "COVERSET_PUBLIC_URL is http://demo.example.org: use the tunnel's https URL"
    ]
    ready = Settings.for_tests(
        data_dir=tmp_path, public_url="https://x.trycloudflare.com", admin_token=admin_token
    )
    assert dev.tunnel_problems(ready) == []
    assert dev.tunnel_problems(None, "boom") == ["boom"]
    assert dev.tunnel_commands("127.0.0.1:9000", "https://x.trycloudflare.com")[0].startswith(
        "ngrok http http://127.0.0.1:9000"
    )


# -- the Modal web module, with modal stubbed out ---------------------------------------------------


def _fake_modal(recorded: dict[str, Any]) -> ModuleType:
    """Just enough of ``modal`` for the module to import and define its function offline."""

    def passthrough(*_args: Any, **_kwargs: Any) -> Callable[[Any], Any]:
        return lambda function: function

    class App:
        def __init__(self, name: str) -> None:
            recorded["app"] = name

        def function(self, **kwargs: Any) -> Callable[[Any], Any]:
            recorded["function"] = kwargs
            return lambda function: function

    module = ModuleType("modal")
    module.App = App  # type: ignore[attr-defined]
    module.Volume = SimpleNamespace(  # type: ignore[attr-defined]
        from_name=lambda name, **_kw: SimpleNamespace(
            name=name, commit=lambda: recorded.setdefault("commits", 0)
        )
    )
    module.Secret = SimpleNamespace(from_name=lambda name: ("secret", name))  # type: ignore[attr-defined]
    module.Image = SimpleNamespace(debian_slim=lambda **_kw: "image")  # type: ignore[attr-defined]
    module.FilePatternMatcher = SimpleNamespace(from_file=lambda _path: None)  # type: ignore[attr-defined]
    module.is_local = lambda: False  # type: ignore[attr-defined]
    module.concurrent = passthrough  # type: ignore[attr-defined]
    module.asgi_app = passthrough  # type: ignore[attr-defined]
    return module


def _load_modal_web(
    monkeypatch: pytest.MonkeyPatch, web_state: str | None = None
) -> tuple[ModuleType, dict[str, Any]]:
    recorded: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "modal", _fake_modal(recorded))
    if "MODAL_IMAGE_BUILDER_VERSION" not in os.environ:  # the module sets a default at import
        _forget_after(monkeypatch, "MODAL_IMAGE_BUILDER_VERSION")
    if web_state is None:
        monkeypatch.delenv("COVERSET_WEB_STATE", raising=False)
    else:
        monkeypatch.setenv("COVERSET_WEB_STATE", web_state)
    spec = importlib.util.spec_from_file_location(
        "coverset_web_stubbed", DEPLOY / "modal" / "coverset_web.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, recorded


class FakeApp:
    """What ``create_app`` returns here: records the settings and the lifespan hooks installed."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.hooks: list[str] = []

    def before_serving(self, function: Any) -> Any:
        self.hooks.append("before_serving")
        return function

    def after_serving(self, function: Any) -> Any:
        self.hooks.append("after_serving")
        return function


@pytest.fixture
def modal_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A clean environment for build_web_app (which writes os.environ and changes directory):
    monkeypatch records every key it may set, so all of them are restored afterwards."""
    monkeypatch.chdir(tmp_path)
    for key in [key for key in os.environ if key.startswith("COVERSET_")]:
        monkeypatch.delenv(key)
    _forget_after(
        monkeypatch,
        "COVERSET_DATA_DIR",
        "COVERSET_DATABASE_URL",
        "COVERSET_CACHE_DIR",
        "COVERSET_TRUST_PROXY",
        "COVERSET_EXECUTOR",
        "COVERSET_MODAL_ENVIRONMENT",
        "COVERSET_ENV",
        "COVERSET_GITHUB_APP_PRIVATE_KEY_PATH",
    )
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_modal_hosted_mode_skips_the_volume_machinery(
    monkeypatch: pytest.MonkeyPatch, modal_env: Path, admin_token: str
) -> None:
    web, recorded = _load_modal_web(monkeypatch)
    assert recorded["function"]["volumes"], "the default deploy mounts the Volume"
    local, volume = modal_env / "local", modal_env / "volume"
    monkeypatch.setenv("COVERSET_DATABASE_URL", "postgresql://coverset:pw@db.internal/coverset")
    monkeypatch.setenv("COVERSET_BLOB_URL", BLOB_URL)
    monkeypatch.setenv("COVERSET_ADMIN_TOKEN", admin_token)
    monkeypatch.setenv("COVERSET_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv(
        web.PRIVATE_KEY_ENV, "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    )
    restored: list[Any] = []
    monkeypatch.setattr(web.SQLiteVolumeBackup, "restore", lambda self: restored.append(self))

    app = web.build_web_app(
        os.environ, volume_mounted=True, local_dir=local, volume_mount=volume, create_app=FakeApp
    )

    assert restored == [] and app.hooks == [], "no restore, no backup timer"
    assert app.settings.hosted is True
    assert app.settings.data_dir == local and app.settings.cache_dir == local / "cache"
    assert Path.cwd() == local, "the working directory is the container's disk, not the Volume"
    assert not volume.exists(), "nothing touches the Volume"
    key = local / "secrets" / "github-app.pem"
    assert app.settings.github_app_private_key_path == key
    assert key.read_text(encoding="utf-8").startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_modal_volume_mode_is_the_fallback(monkeypatch: pytest.MonkeyPatch, modal_env: Path) -> None:
    web, _ = _load_modal_web(monkeypatch)
    local, volume = modal_env / "local", modal_env / "volume"
    restored: list[Any] = []
    monkeypatch.setattr(web.SQLiteVolumeBackup, "restore", lambda self: restored.append(self) or "restored")

    app = web.build_web_app(
        os.environ, volume_mounted=True, local_dir=local, volume_mount=volume, create_app=FakeApp
    )

    assert len(restored) == 1 and restored[0].live == local / "coverset.db"
    assert restored[0].backup_dir == volume / "backup"
    assert app.hooks == ["before_serving", "after_serving"], "the backup timer and the final backup"
    assert app.settings.hosted is False
    assert app.settings.data_dir == volume and Path.cwd() == volume
    assert app.settings.database_url == f"sqlite+aiosqlite:///{local / 'coverset.db'}"


def test_modal_postgres_without_a_bucket_keeps_the_volume_for_files(
    monkeypatch: pytest.MonkeyPatch, modal_env: Path
) -> None:
    web, _ = _load_modal_web(monkeypatch)
    monkeypatch.setenv("COVERSET_DATABASE_URL", PG_URL)
    plan = web.plan_container(os.environ, local_dir=modal_env / "l", volume_mount=modal_env / "v")
    assert (plan.hosted, plan.backups, plan.workdir) == (False, False, modal_env / "v")


def test_modal_without_a_volume_refuses_a_stateful_secret(
    monkeypatch: pytest.MonkeyPatch, modal_env: Path
) -> None:
    web, recorded = _load_modal_web(monkeypatch, web_state="hosted")
    assert recorded["function"]["volumes"] == {}, "COVERSET_WEB_STATE=hosted deploys no Volume"
    assert recorded["function"]["max_containers"] == 1
    with pytest.raises(RuntimeError, match="no Volume"):
        web.build_web_app(
            os.environ,
            volume_mounted=False,
            local_dir=modal_env / "local",
            volume_mount=modal_env / "volume",
            create_app=FakeApp,
        )


def test_secret_is_hosted_matches_settings_hosted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    web, _ = _load_modal_web(monkeypatch)
    cases = [
        ({"COVERSET_DATABASE_URL": "postgres://u:p@h/db", "COVERSET_BLOB_URL": BLOB_URL}, True),
        ({"COVERSET_DATABASE_URL": PG_URL, "COVERSET_BLOB_URL": BLOB_URL}, True),
        ({"COVERSET_DATABASE_URL": PG_URL}, False),
        ({"COVERSET_BLOB_URL": BLOB_URL}, False),
        ({}, False),
    ]
    for environ, expected in cases:
        assert web.secret_is_hosted(environ) is expected
        fields = {k.removeprefix("COVERSET_").lower(): v for k, v in environ.items()}
        assert Settings.for_tests(data_dir=tmp_path, **fields).hosted is expected


# -- the deployment files ----------------------------------------------------------------------------


def _compose(name: str) -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / name).read_text(encoding="utf-8"))


def test_compose_has_an_optional_hosted_profile() -> None:
    compose = _compose("compose.yml")
    services = compose["services"]
    assert set(services) == {"coverset", "caddy", "postgres", "minio", "minio-init"}
    for name in ("postgres", "minio", "minio-init"):
        assert services[name]["profiles"] == ["hosted"], name
    for name in ("coverset", "caddy"):
        assert "profiles" not in services[name], f"{name} runs without the profile"
    depends = services["coverset"]["depends_on"]
    assert depends["postgres"]["required"] is False and depends["minio-init"]["required"] is False
    assert services["postgres"]["image"] == "postgres:17"
    assert "ports" not in services["postgres"] and "ports" not in services["minio"], "never published"
    assert {"coverset-postgres", "coverset-minio"} <= set(compose["volumes"])
    init = " ".join(services["minio-init"]["entrypoint"])
    assert "mc version enable" in init
    assert "NoncurrentVersionExpiration" in init and "mc ilm import" in init, (
        "a versioned bucket without the rule keeps everything gc deletes"
    )
    assert services["minio-init"]["environment"]["COVERSET_MINIO_NONCURRENT_DAYS"].endswith(":-30}")


def test_dev_compose_names_the_services_dev_uses() -> None:
    services = _compose("dev-compose.yml")["services"]
    assert set(dev.DEV_SERVICES) <= set(services)
    ports = " ".join(services["postgres"]["ports"] + services["minio"]["ports"])
    for variable in ("COVERSET_DEV_PG_PORT", "COVERSET_DEV_S3_PORT", "COVERSET_DEV_S3_CONSOLE_PORT"):
        assert variable in ports, "DevStack.from_environ reads the same variables"
    assert services["postgres"]["environment"]["POSTGRES_USER"] == dev.DEV_PG_CREDENTIALS[0]
    assert services["minio"]["environment"]["MINIO_ROOT_USER"] == dev.DEV_S3_CREDENTIALS[0]
    assert dev.DEV_COMPOSE_FILE == DEPLOY / "dev-compose.yml"


def test_fly_config_is_stateless() -> None:
    fly = tomllib.loads((DEPLOY / "fly" / "fly.toml").read_text(encoding="utf-8"))
    assert "mounts" not in fly, "no volume: the state is in Postgres and Tigris"
    assert fly["env"]["COVERSET_CACHE_DIR"] == "/var/cache/coverset"
    assert fly["env"]["COVERSET_MIGRATE_ON_START"] == "true"
    assert fly["http_service"]["min_machines_running"] == 1
    assert fly["http_service"]["auto_stop_machines"] == "off"


def test_aws_policy_is_least_privilege() -> None:
    policy = json.loads((DEPLOY / "aws" / "iam-policy.json").read_text(encoding="utf-8"))
    actions = {action for statement in policy["Statement"] for action in statement["Action"]}
    assert actions == {
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
    }
    for statement in policy["Statement"]:
        assert (
            statement["Effect"] == "Allow" and "*" not in statement["Resource"].split(":::")[1].split("/")[0]
        )
    object_resources = [s["Resource"] for s in policy["Statement"] if "s3:GetObject" in s["Action"]]
    assert object_resources == ["arn:aws:s3:::coverset-prod/coverset/*"], "objects under the prefix only"


def test_ecs_task_definition_keeps_secrets_out_of_the_environment() -> None:
    task = json.loads((DEPLOY / "aws" / "ecs-task-definition.json").read_text(encoding="utf-8"))
    container = task["containerDefinitions"][0]
    plain = {item["name"] for item in container["environment"]}
    secret = {item["name"] for item in container["secrets"]}
    assert {"COVERSET_DATABASE_URL", "COVERSET_ADMIN_TOKEN", "COVERSET_SECRET_KEY"} <= secret
    assert not plain & secret
    assert "COVERSET_BLOB_URL" in plain and task["taskRoleArn"], "the bucket through the task role"
    assert not any(name.startswith("AWS_") for name in plain | secret), "no static AWS keys on AWS"


def test_dockerfile_runs_the_entrypoint_with_the_cache_on_the_container_disk() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "git ca-certificates tini" in dockerfile
    assert "COVERSET_CACHE_DIR=/var/cache/coverset" in dockerfile
    assert 'VOLUME ["/data"]' in dockerfile and '/var/cache/coverset"]' not in dockerfile
    assert "COVERSET_MIGRATE_ON_START=true" in dockerfile
    assert 'ENTRYPOINT ["tini", "--", "coverset-entrypoint"]' in dockerfile
    assert "!deploy/docker-entrypoint.sh" in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")


def test_entrypoint_writes_the_key_and_execs_coverset(tmp_path: Path) -> None:
    """Run the entrypoint with a fake ``coverset`` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "coverset"
    fake.write_text('#!/bin/sh\necho "coverset $*"\n', encoding="utf-8")
    fake.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "COVERSET_GITHUB_APP_PRIVATE_KEY": "-----BEGIN KEY-----\nline\n-----END KEY-----",
        "COVERSET_GITHUB_APP_PRIVATE_KEY_PATH": str(tmp_path / "secrets" / "app.pem"),
    }
    done = subprocess.run(
        ["sh", str(DEPLOY / "docker-entrypoint.sh"), "init-db"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "coverset init-db"
    key = tmp_path / "secrets" / "app.pem"
    assert key.read_text(encoding="utf-8") == "-----BEGIN KEY-----\nline\n-----END KEY-----\n"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    bare = subprocess.run(
        ["sh", str(DEPLOY / "docker-entrypoint.sh"), "serve"],
        env={"PATH": env["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert bare.stdout.strip() == "coverset serve"


def test_hosted_dev_example_is_the_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``.env.hosted-dev.example`` parses as a .env into hosted settings that match --hosted."""
    monkeypatch.chdir(tmp_path)
    for key in [key for key in os.environ if key.startswith(("COVERSET_", "AWS_"))]:
        monkeypatch.delenv(key)
    (tmp_path / ".env").write_text((PROJECT_ROOT / ".env.hosted-dev.example").read_text(encoding="utf-8"))
    settings = Settings()
    stack = dev.DevStack()
    assert settings.hosted
    assert settings.database_url == stack.database_url
    assert settings.blob_url == stack.blob_url
    assert settings.data_dir == (tmp_path / dev.DEV_HOSTED_DATA_DIR).resolve()


def test_modal_max_containers_is_a_hosted_only_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COVERSET_WEB_MAX_CONTAINERS", "3")
    _, volume_mode = _load_modal_web(monkeypatch)
    assert volume_mode["function"]["max_containers"] == 1, "SQLite on a Volume: one writer"
    _, hosted = _load_modal_web(monkeypatch, web_state="hosted")
    assert hosted["function"]["max_containers"] == 3


def test_a_key_file_is_called_out_when_the_data_dir_is_disposable(tmp_path: Path) -> None:
    laptop = Settings.for_tests(data_dir=tmp_path / "a")
    hosted = Settings.for_tests(data_dir=tmp_path / "b", database_url=PG_URL, blob_url=BLOB_URL)
    assert "lost with the container" not in _checks(laptop)["secret key"].detail
    assert "set COVERSET_SECRET_KEY" in _checks(hosted)["secret key"].detail


def test_modal_exec_shells_see_the_hosted_layout(
    monkeypatch: pytest.MonkeyPatch, modal_env: Path, admin_token: str
) -> None:
    """A hosted deploy's image carries the layout web() uses, so `modal container exec ... coverset
    deploy check` checks the server's data dir, cache and key path, not a /data with no Volume."""
    web, _ = _load_modal_web(monkeypatch, web_state="hosted")
    local = modal_env / "local"
    volume_env = web.image_environment("volume", local_dir=local)
    assert volume_env == web.container_environment(local_dir=local), "the Volume mode's image is unchanged"
    image_env = web.image_environment("hosted", local_dir=local)
    assert image_env["COVERSET_DATA_DIR"] == str(local)
    assert image_env["COVERSET_CACHE_DIR"] == str(local / "cache")
    secret = {
        "COVERSET_DATABASE_URL": PG_URL,
        "COVERSET_BLOB_URL": BLOB_URL,
        "COVERSET_ADMIN_TOKEN": admin_token,
        "COVERSET_PUBLIC_URL": PUBLIC_URL,
        web.PRIVATE_KEY_ENV: "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
    }
    for key, value in {**image_env, **secret}.items():  # what an exec shell's environment holds
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    shell = Settings()
    app = web.build_web_app(
        os.environ, volume_mounted=False, local_dir=local, volume_mount=modal_env / "v", create_app=FakeApp
    )
    served = app.settings
    assert (
        (shell.data_dir, shell.cache_dir) == (served.data_dir, served.cache_dir) == (local, local / "cache")
    )
    assert shell.github_app_private_key_path == served.github_app_private_key_path
    assert shell.hosted and served.hosted
