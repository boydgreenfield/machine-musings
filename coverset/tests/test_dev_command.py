"""``coverset dev``, ``coverset serve --reload``, ``coverset demo --fresh`` and the quiet migrations.

The watcher runs on a fake clock and a fake scan (no sleeping); the reloader on a fake child
process. The development sign-in's conditions are checked here as a pure function of the
settings; tests/web/test_dev_login.py drives the route itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from coverset.cli import cli
from coverset.commands import dev
from coverset.commands.demo import DATA_DIR_MARKER, wipe_data_dir
from coverset.commands.dev import (
    Reloader,
    Watcher,
    changed_paths,
    describe_change,
    health_url,
    live_reloadable,
    scan_mtimes,
    watch_roots,
)
from coverset.config import Settings
from coverset.db.session import LOG_LEVEL_ENV, init_db, quiet_alembic
from coverset.web.account import dev_login_problems

# -- the watcher -----------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeScan:
    def __init__(self, files: dict[str, int] | None = None) -> None:
        self.files = dict(files or {"a.py": 1, "b.html": 1})
        self.calls = 0

    def __call__(self) -> dict[str, int]:
        self.calls += 1
        return dict(self.files)


def make_watcher(scan: FakeScan, clock: FakeClock) -> Watcher:
    return Watcher(scan=scan, clock=clock, debounce_s=0.3, poll_interval_s=0.5)


def test_nothing_changed_reports_nothing() -> None:
    scan, clock = FakeScan(), FakeClock()
    watcher = make_watcher(scan, clock)
    for _ in range(5):
        clock.advance(0.5)
        assert watcher.poll() == []
    assert watcher.next_wait() == 0.5


def test_a_change_is_reported_once_after_the_debounce() -> None:
    scan, clock = FakeScan(), FakeClock()
    watcher = make_watcher(scan, clock)
    scan.files["a.py"] = 2
    clock.advance(0.5)
    assert watcher.poll() == []  # seen, still settling
    assert watcher.next_wait() == pytest.approx(0.3)
    clock.advance(0.2)
    assert watcher.poll() == []  # 0.2 s quiet: not yet
    assert watcher.next_wait() == pytest.approx(0.1)
    clock.advance(0.1)
    assert watcher.poll() == ["a.py"]
    clock.advance(0.5)
    assert watcher.poll() == []  # reported once


def test_changes_keep_extending_the_debounce_and_are_reported_together() -> None:
    scan, clock = FakeScan(), FakeClock()
    watcher = make_watcher(scan, clock)
    scan.files["a.py"] = 2
    clock.advance(0.1)
    assert watcher.poll() == []
    for step in range(3, 6):  # a save-all: a change every 0.2 s
        scan.files["b.html"] = step
        clock.advance(0.2)
        assert watcher.poll() == []
    scan.files["c.py"] = 1  # added
    del scan.files["a.py"]  # removed
    clock.advance(0.2)
    assert watcher.poll() == []
    clock.advance(0.3)
    assert watcher.poll() == ["a.py", "b.html", "c.py"]


def test_changed_paths_covers_added_removed_and_modified() -> None:
    before = {"same": 1, "edited": 1, "gone": 1}
    after = {"same": 1, "edited": 2, "new": 1}
    assert changed_paths(before, after) == ["edited", "gone", "new"]


def test_scan_watches_sources_and_skips_caches_and_hidden_entries(tmp_path: Path) -> None:
    keep = ["pkg/mod.py", "pkg/web/templates/page.html", "pkg/web/static/app.css", "pkg/web/static/x.js"]
    skip = [
        "pkg/__pycache__/mod.cpython-313.pyc",
        "pkg/__pycache__/mod.py",
        "pkg/node_modules/lib/index.js",
        "pkg/.venv/site.py",
        "pkg/.hidden.py",
        "pkg/.cache/x.py",
        "pkg/notes.md",
        "pkg/data.json",
        "pkg/image.png",
        "pkg/target/debug/build.js",
    ]
    for rel in keep + skip:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    (tmp_path / "pkg" / "linked").symlink_to(tmp_path / "pkg" / "web", target_is_directory=True)
    found = scan_mtimes([tmp_path / "pkg", tmp_path / "missing"])
    assert sorted(Path(p).relative_to(tmp_path).as_posix() for p in found) == sorted(keep)
    stamp = os.stat(tmp_path / keep[0]).st_mtime_ns
    assert found[str(tmp_path / keep[0])] == stamp


def test_a_real_edit_is_seen_by_the_scan(tmp_path: Path) -> None:
    source = tmp_path / "mod.py"
    source.write_text("a = 1\n")
    clock = FakeClock()
    watcher = Watcher(scan=lambda: scan_mtimes([tmp_path]), clock=clock)
    source.write_text("a = 2\n")
    os.utime(source, ns=(1, 1))  # a different mtime even on coarse filesystems
    clock.advance(0.5)
    assert watcher.poll() == []
    clock.advance(0.3)
    assert watcher.poll() == [str(source)]


def test_watch_roots_are_the_package_and_the_workspace_sources() -> None:
    roots = watch_roots()
    package = Path(dev.__file__).resolve().parents[1]
    assert roots[0] == package
    project = package.parent.parent
    assert project / "packages" / "pytest-coverset" / "src" in roots
    assert project / "packages" / "coverset-model" / "src" in roots


def test_templates_and_static_files_reload_without_a_restart() -> None:
    package = Path(dev.__file__).resolve().parents[1]
    assert live_reloadable(package / "web" / "templates" / "repo.html")
    assert live_reloadable(package / "web" / "static" / "app.css")
    assert live_reloadable(package / "web" / "static" / "htmx.min.js")
    assert not live_reloadable(package / "web" / "pages.py")
    assert not live_reloadable(package / "report" / "templates" / "index.html")  # a cached environment
    assert not live_reloadable(package / "web" / "templates" / "stray.py")


def test_describe_change_and_health_url() -> None:
    assert describe_change(["/x/a.py"]) == "/x/a.py"
    assert describe_change(["/x/a.py", "/x/b.py", "/x/c.py"]) == "/x/a.py (and 2 more)"
    assert health_url("127.0.0.1:8123") == "http://127.0.0.1:8123/healthz"
    assert health_url("0.0.0.0:80") == "http://127.0.0.1:80/healthz"
    assert health_url("[::1]:8000") == "http://[::1]:8000/healthz"
    assert health_url("[::]:8000") == "http://[::1]:8000/healthz"


# -- the reloader ----------------------------------------------------------------------------------


class FakeChild:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = -15

    def wait(self, timeout: float | None = None) -> int | None:
        return self.code

    def kill(self) -> None:
        self.code = -9


def make_reloader(scan: FakeScan, clock: FakeClock) -> tuple[Reloader, list[FakeChild], list[str]]:
    children: list[FakeChild] = []
    lines: list[str] = []

    def spawn(argv: list[str]) -> Any:
        child = FakeChild(argv)
        children.append(child)
        return child

    reloader = Reloader(
        "127.0.0.1:1",  # nothing answers: the readiness thread gives up when the child "exits"
        watcher=make_watcher(scan, clock),
        echo=lines.append,
        spawn=spawn,
    )
    return reloader, children, lines


def settle(reloader: Reloader, clock: FakeClock, steps: int = 3) -> None:
    iterator = reloader.steps()
    for _ in range(steps):
        next(iterator)
        clock.advance(0.3)


def test_a_python_change_restarts_the_child_with_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    scan, clock = FakeScan({"/src/coverset/web/pages.py": 1}), FakeClock()
    reloader, children, lines = make_reloader(scan, clock)
    reloader.start()
    assert len(children) == 1
    assert children[0].argv[-1] == "127.0.0.1:1" and "serve_child" in children[0].argv[2]
    scan.files["/src/coverset/web/pages.py"] = 2
    settle(reloader, clock)
    assert len(children) == 2 and children[0].terminated and not children[1].terminated
    assert lines == ["coverset dev: /src/coverset/web/pages.py changed; restarting the server"]
    assert reloader.restarts == 1
    children[1].code = 0  # let the readiness threads finish


def test_a_template_change_needs_no_restart() -> None:
    template = str(Path(dev.__file__).resolve().parents[1] / "web" / "templates" / "repo.html")
    scan, clock = FakeScan({template: 1}), FakeClock()
    reloader, children, lines = make_reloader(scan, clock)
    reloader.start()
    scan.files[template] = 2
    settle(reloader, clock)
    assert len(children) == 1 and not children[0].terminated
    assert len(lines) == 1 and lines[0].endswith("changed; served fresh, no restart")
    children[0].code = 0


def test_a_crashed_child_is_reported_once_and_started_on_the_next_change() -> None:
    scan, clock = FakeScan({"/src/a.py": 1}), FakeClock()
    reloader, children, lines = make_reloader(scan, clock)
    reloader.start()
    children[0].code = 1
    settle(reloader, clock, steps=4)
    assert lines == ["coverset dev: the server exited with code 1; save a file to restart it"]
    scan.files["/src/a.py"] = 2
    settle(reloader, clock)
    assert len(children) == 2
    assert lines[-1] == "coverset dev: /src/a.py changed; starting the server"
    children[1].code = 0


def test_stop_kills_a_child_that_ignores_sigterm() -> None:
    class Stubborn(FakeChild):
        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int | None:
            if self.code is None:
                import subprocess

                raise subprocess.TimeoutExpired("child", timeout or 0)
            return self.code

    child = Stubborn([])
    reloader = Reloader("127.0.0.1:1", watcher=make_watcher(FakeScan(), FakeClock()), echo=lambda _: None)
    reloader.child = child  # type: ignore[assignment]
    reloader.stop()
    assert child.terminated and child.code == -9 and reloader.child is None


# -- the commands ----------------------------------------------------------------------------------


def test_dev_and_serve_reload_are_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["dev", "--help"])
    assert result.exit_code == 0
    assert "--no-seed" in result.output and "--bind" in result.output and "--open" in result.output
    serve = runner.invoke(cli, ["serve", "--help"])
    assert "--reload" in serve.output
    demo = runner.invoke(cli, ["demo", "--help"])
    assert "--fresh" in demo.output


def test_serve_reload_runs_the_reloader_without_seeding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        dev, "run_reloader", lambda bind, open_browser=False: calls.append((bind, open_browser))
    )
    result = CliRunner().invoke(
        cli, ["serve", "--reload", "--bind", "127.0.0.1:8124"], env={"COVERSET_DATA_DIR": str(tmp_path)}
    )
    assert result.exit_code == 0, result.output
    assert calls == [("127.0.0.1:8124", False)]
    assert not (tmp_path / "coverset.db").exists()


def test_dev_seeds_an_empty_data_dir_and_skips_a_seeded_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeded: list[Path] = []
    served: list[tuple[str, bool]] = []
    monkeypatch.setattr(dev, "seed_in_child", lambda: seeded.append(Path(os.environ["COVERSET_DATA_DIR"])))
    monkeypatch.setattr(
        dev, "run_reloader", lambda bind, open_browser=False: served.append((bind, open_browser))
    )
    env = {"COVERSET_DATA_DIR": str(tmp_path / "data")}
    runner = CliRunner()
    result = runner.invoke(cli, ["dev", "--bind", "127.0.0.1:8125", "--open"], env=env)
    assert result.exit_code == 0, result.output
    assert seeded == [tmp_path / "data"]
    assert served == [("127.0.0.1:8125", True)]
    assert "seeding the demo" in result.output

    result = runner.invoke(cli, ["dev", "--no-seed"], env=env)
    assert result.exit_code == 0 and "--no-seed" in result.output
    assert len(seeded) == 1 and served[-1] == ("127.0.0.1:8000", False)


def test_seeding_runs_in_a_child_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo seeds in a separate process, so its process pool never lives in the reloader."""
    import subprocess
    import sys

    argv = dev.seed_command()
    assert argv[0] == sys.executable and argv[1] == "-c"
    assert "seed_demo_data(demo_settings('local'), report=False)" in argv[2]
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    dev.seed_in_child()
    assert calls == [argv]


def test_a_failed_seed_stops_dev_before_serving(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    served: list[str] = []
    monkeypatch.setattr(dev.subprocess, "run", lambda command, check: subprocess.CompletedProcess(command, 3))
    monkeypatch.setattr(dev, "run_reloader", lambda bind, open_browser=False: served.append(bind))
    result = CliRunner().invoke(cli, ["dev"], env={"COVERSET_DATA_DIR": str(tmp_path / "data")})
    assert result.exit_code == 1
    assert "seeding the demo failed (exit 3)" in result.output
    assert served == []


def test_dev_says_whether_the_development_sign_in_is_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dev, "run_reloader", lambda bind, open_browser=False: None)
    env = {"COVERSET_DATA_DIR": str(tmp_path), "COVERSET_DEV_LOGIN": "true", "COVERSET_FORGE": "fake"}
    on = CliRunner().invoke(cli, ["dev", "--no-seed", "--bind", "127.0.0.1:8126"], env=env)
    assert "development sign-in on at http://127.0.0.1:8126/login" in on.output
    off = CliRunner().invoke(cli, ["dev", "--no-seed"], env={**env, "COVERSET_ENV": "prod"})
    assert "development sign-in off: COVERSET_ENV is prod, not dev" in off.output
    quiet = CliRunner().invoke(cli, ["dev", "--no-seed"], env={"COVERSET_DATA_DIR": str(tmp_path)})
    assert "development sign-in" not in quiet.output


def test_data_dir_has_repository(tmp_path: Path) -> None:
    from coverset.db.models import Repo
    from coverset.db.session import Database

    settings = Settings.for_tests(data_dir=tmp_path / "data")
    assert not dev.data_dir_has_repository(settings)  # no database file
    asyncio.run(init_db(settings))
    assert not dev.data_dir_has_repository(settings)  # schema, no repository

    async def add_repo() -> None:
        database = Database.from_url(settings.database_url)
        async with database.session() as session:
            session.add(Repo(forge="local", external_id="x", full_name="demo/ledger"))
            await session.commit()
        await database.engine.dispose()

    asyncio.run(add_repo())
    assert dev.data_dir_has_repository(settings)


def test_enable_dev_reload_turns_on_template_auto_reload(tmp_path: Path) -> None:
    from coverset.app import create_app

    app = create_app(Settings.for_tests(data_dir=tmp_path / "data"))
    assert not app.jinja_env.auto_reload
    dev.enable_dev_reload(app)
    assert app.config["TEMPLATES_AUTO_RELOAD"] is True
    assert app.jinja_env.auto_reload is True


async def test_an_edited_template_is_served_fresh_with_auto_reload(tmp_path: Path) -> None:
    """Quart honours the flags even after its Jinja environment exists: with them a template changed
    on disk is re-read by the next render, without them the cached one is served."""
    from quart import Quart, render_template

    async def render_twice(reload: bool) -> tuple[str, str]:
        folder = tmp_path / f"templates-{reload}"
        folder.mkdir()
        page = folder / "probe.html"
        page.write_text("one")
        app = Quart("probe", template_folder=str(folder))
        assert app.jinja_env is not None  # created before the flags are set, as in create_app
        if reload:
            dev.enable_dev_reload(app)
        async with app.app_context():
            first = await render_template("probe.html")
            page.write_text("two")
            os.utime(page, ns=(10**18, 10**18))
            return first, await render_template("probe.html")

    assert await render_twice(reload=True) == ("one", "two")
    assert await render_twice(reload=False) == ("one", "one")


# -- demo --fresh ----------------------------------------------------------------------------------


def fresh_settings(data_dir: Path, **overrides: Any) -> Settings:
    return Settings.for_tests(data_dir=data_dir, **overrides)


def test_fresh_empties_a_coverset_data_dir(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "mirrors" / "1.git").mkdir(parents=True)
    (data / "facts" / "repo=1").mkdir(parents=True)
    (data / DATA_DIR_MARKER).write_text("db")
    (data / "secret_key").write_text("k")
    outside = tmp_path / "keep.txt"
    outside.write_text("keep")
    (data / "link").symlink_to(outside)
    assert wipe_data_dir(fresh_settings(data)) == 5
    assert data.is_dir() and list(data.iterdir()) == []
    assert outside.read_text() == "keep"  # a symlink is unlinked, never followed


def test_fresh_accepts_an_empty_or_missing_dir(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert wipe_data_dir(fresh_settings(tmp_path / "empty")) == 0
    assert wipe_data_dir(fresh_settings(tmp_path / "missing")) == 0


def test_fresh_refuses_a_directory_that_is_not_a_coverset_data_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "thesis.tex").write_text("years of work")
    with pytest.raises(click.ClickException, match="does not look like a coverset data directory"):
        wipe_data_dir(fresh_settings(home))
    assert (home / "thesis.tex").read_text() == "years of work"


def test_fresh_refuses_a_symlinked_data_dir_and_a_database_elsewhere(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / DATA_DIR_MARKER).write_text("db")
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    settings = fresh_settings(real)
    settings.data_dir = tmp_path / "alias"  # what the validator would have resolved away
    with pytest.raises(click.ClickException, match="symlink"):
        wipe_data_dir(settings)
    elsewhere = fresh_settings(real, database_url=f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    with pytest.raises(click.ClickException, match="not a SQLite file inside"):
        wipe_data_dir(elsewhere)
    assert (real / DATA_DIR_MARKER).exists()


def test_demo_fresh_command_refuses_before_doing_anything(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("mine")
    result = CliRunner().invoke(
        cli, ["demo", "--fresh", "--no-serve"], env={"COVERSET_DATA_DIR": str(tmp_path)}
    )
    assert result.exit_code == 1
    assert "does not look like a coverset data directory" in result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt"]


# -- the development sign-in's conditions ----------------------------------------------------------


def dev_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"env": "dev", "dev_login": True}
    values.update(overrides)
    return Settings.for_tests(**values)


def test_dev_login_conditions_all_hold() -> None:
    assert dev_login_problems(dev_settings()) == []


def test_dev_login_is_off_by_default() -> None:
    assert Settings.for_tests().dev_login is False
    assert dev_login_problems(dev_settings(dev_login=False)) == ["COVERSET_DEV_LOGIN is not true"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"env": "prod"}, "COVERSET_ENV is prod, not dev"),
        ({"env": "test"}, "COVERSET_ENV is test, not dev"),
        ({"bind": "0.0.0.0:8000", "admin_token": "t" * 40}, "is not a loopback address"),
        ({"bind": "192.168.1.5:8000", "admin_token": "t" * 40}, "is not a loopback address"),
        ({"public_url": "https://ci.example.com", "admin_token": "t" * 40}, "reachable from outside"),
        ({"github_token": SecretStr("ghp_" + "a" * 36)}, "GitHub credentials are configured"),
        ({"github_token": SecretStr("ghp_" + "a" * 36), "forge": "fake"}, "COVERSET_GITHUB_TOKEN"),
        ({"github_app_client_secret": SecretStr("s" * 40)}, "COVERSET_GITHUB_APP_CLIENT_SECRET"),
        ({"github_app_id": 1, "allowed_owners": ["acme"]}, "COVERSET_GITHUB_APP_ID"),
        ({"forge": "github"}, "not the fake forge"),
    ],
)
def test_dev_login_is_off_when_any_condition_fails(overrides: dict[str, Any], expected: str) -> None:
    problems = dev_login_problems(dev_settings(**overrides))
    assert any(expected in problem for problem in problems), problems


def test_dev_login_private_public_urls_are_fine() -> None:
    for url in ("http://localhost:8000", "http://coverset.localhost:8000", "http://coverset.test"):
        assert dev_login_problems(dev_settings(public_url=url)) == [], url


# -- quiet migrations ------------------------------------------------------------------------------


def test_alembic_is_quiet_unless_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("alembic")
    previous = logger.level
    try:
        monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
        quiet_alembic()
        assert logger.level == logging.WARNING
        monkeypatch.setenv(LOG_LEVEL_ENV, "debug")
        quiet_alembic()
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(previous)


def test_init_db_returns_the_applied_revisions_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings.for_tests(data_dir=tmp_path / "data")
    with caplog.at_level(logging.INFO):
        applied = asyncio.run(init_db(settings))
    assert applied[0].startswith("0001 ") and len(applied) >= 8
    assert [line.split()[0] for line in applied] == sorted(line.split()[0] for line in applied)
    assert not [r for r in caplog.records if r.name.startswith("alembic") and r.levelno < logging.WARNING]
    # every real step still reaches the log (the server's startup migration has no init-db output)
    logged = [r.getMessage() for r in caplog.records if r.name == "coverset.db.session"]
    assert logged == [f"applied migration {line}" for line in applied]
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert asyncio.run(init_db(settings)) == []
    assert not [r for r in caplog.records if r.name == "coverset.db.session"]


def test_init_db_command_prints_one_line_per_revision(tmp_path: Path) -> None:
    env = {"COVERSET_DATA_DIR": str(tmp_path / "data")}
    first = CliRunner().invoke(cli, ["init-db"], env=env)
    assert first.exit_code == 0, first.output
    lines = first.output.splitlines()
    applied = [line for line in lines if line.startswith("applied migration ")]
    assert applied[0].startswith("applied migration 0001 ") and len(applied) >= 8
    assert "Context impl" not in first.output and "non-transactional" not in first.output
    assert lines[-1].startswith(f"database ready ({len(applied)} migration(s) applied): sqlite")
    again = CliRunner().invoke(cli, ["init-db"], env=env)
    assert again.output.startswith("database ready (already at the latest revision)")


async def test_an_edited_stylesheet_gets_a_new_version_url(tmp_path: Path) -> None:
    """Static URLs carry a content hash recomputed on change, so a dev edit to app.css is fetched
    fresh on the next page load while the old URL stays cacheable forever."""
    from quart import Quart, url_for

    from coverset.app import STATIC_IMMUTABLE, STATIC_REVALIDATE, install_response_caching

    static = tmp_path / "static"
    static.mkdir()
    css = static / "app.css"
    css.write_text("body { color: red; }")
    app = Quart("probe", static_folder=str(static))
    install_response_caching(app)
    async with app.test_request_context("/"):
        first = url_for("static", filename="app.css")
    css.write_text("body { color: blue; }  /* edited */")
    os.utime(css, ns=(10**18, 10**18))
    async with app.test_request_context("/"):
        second = url_for("static", filename="app.css")
    assert first != second and "?v=" in first and "?v=" in second
    client = app.test_client()
    fresh = await client.get(second)
    assert fresh.headers["Cache-Control"] == STATIC_IMMUTABLE
    assert "blue" in await fresh.get_data(as_text=True)
    stale = await client.get(first)
    assert stale.headers["Cache-Control"] == STATIC_REVALIDATE
