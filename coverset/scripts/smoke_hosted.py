"""Smoke-test a hosted coverset state: Postgres + an S3-compatible bucket, end to end.

Point it at the real thing with the same settings the app uses, and it exercises every path that
reads or writes the shared state, then prints one PASS/FAIL row per step:

    COVERSET_DATABASE_URL=postgresql+asyncpg://... \\
    COVERSET_BLOB_URL='s3://bucket/prefix?endpoint_url=...&region=...' \\
    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
    uv run python scripts/smoke_hosted.py            # --clean afterwards deletes what it made

1. **migrate**: ``coverset init-db`` against the database (the applied revisions, or "at head").
2. **demo**: ``coverset demo --no-serve`` with the local executor: the demo repository, six
   ``main`` baselines and two PRs, every run's facts written through the configured store.
3. **serve**: ``coverset serve`` on a free loopback port (its own process, so it reads the state
   the way a fresh container does) and ``GET /healthz``.
4. **pages**: the dashboard, PR #2, the source view of ``parse.py`` and the Economy page, each
   with the admin token as a bearer (generated for this process when none is set).
5. **upload**: a repository token straight in the database, ``pytest --coverset`` in a checkout
   of PR #2's head, then ``coverset-ci upload --with-sources --wait`` to ``POST /v1/bundles``.
6. **bucket**: the upload run's keys under ``facts/repo=<id>/run=<id>/`` listed from the store
   (from the bucket itself when ``COVERSET_BLOB_URL`` is set; never the local cache).
7. **rows**: the run's row, its test results and its events read back from the database.

The data stays in place for a look in the UI (or psql and the bucket console). ``--clean`` (after a
run) or ``--clean-only`` deletes it: ``coverset repo purge coverset-demo/ledger --yes`` for the
rows and the store's files, then any key left under the repository's prefixes, the smoke's data
directory and the scratch checkout. Without ``COVERSET_BLOB_URL`` it runs against files under its
data directory (how its tests drive it) and says so in a ``state`` row; nothing in it is specific to
one provider.

**Its own data directory.** The demo's git repository is a local directory whose absolute path the
database stores as the repository's source, and the object-store cache is keyed by repository id,
which restarts at 1 in every database. So the smoke never uses the laptop's ``data/`` (nor a
``COVERSET_DATA_DIR`` or ``COVERSET_CACHE_DIR`` from the environment or ``.env``): every child gets
``--data-dir``, by default ``data/smoke-hosted/<digest of the target>``, one fixed directory per
database and bucket so a later ``--clean-only`` finds it, with the cache inside it. The first step
(``data dir``) claims it by writing :data:`MARKER`, and refuses a directory that holds files without
the marker, and a database that already holds ``coverset-demo/ledger`` the smoke did not make (the
demo would repoint that repository at the smoke's git repository, which ``--clean`` then deletes).
``--clean`` touches nothing without the marker for the same target: it purges only what it made.

Every child process gets ``COVERSET_EXECUTOR=local`` and ``COVERSET_FORGE=fake``: no Modal sandbox
is created and nothing is posted to GitHub, whatever else the environment configures. The admin
token, the repository token and the database password never appear in the output.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from coverset.config import Settings

DEMO_REPO = "coverset-demo/ledger"
SOURCE_PATH = "src/ledger/durations/parse.py"
PR_NUMBER = 2
SERVE_TIMEOUT_S = 60.0
UPLOAD_TIMEOUT_S = 240
CHILD_ENV_DROP = ("GITHUB_", "ACTIONS_")
"""Prefixes removed from the children's environment (``coverset-ci`` would ask Actions for OIDC)."""
SMOKE_DATA_ROOT = Path("data") / "smoke-hosted"
"""Parent of the default data directories, one per target (:func:`default_data_dir`)."""
MARKER = ".coverset-smoke.json"
"""Written into the data directory by the ``data dir`` step: the smoke made this directory (and the
demo repository in the recorded target), so ``--clean`` may delete them."""

PAGES: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "/", "ledger"),
    ("PR page", f"/repos/{{rid}}/prs/{PR_NUMBER}", "check_if_new_user_signup_allowed"),
    ("source view", f"/repos/{{rid}}/files/{SOURCE_PATH}", "parse_duration"),
    ("economy", "/repos/{rid}/economy", "leverage"),
)
"""``(name, path, text the page must contain)``; ``{rid}`` is the demo repository's id."""


@dataclass
class Step:
    """One row of the report."""

    name: str
    ok: bool
    detail: str
    seconds: float = 0.0
    warn: bool = False

    @property
    def mark(self) -> str:
        return "FAIL" if not self.ok else "WARN" if self.warn else "PASS"


@dataclass
class Smoke:
    """The run's state: the settings, the children's environment, the steps so far."""

    settings: Settings
    env: dict[str, str]
    workdir: Path
    admin_token: str
    echo: Callable[[str], None] = print
    target: dict[str, str] = field(default_factory=dict)
    """:func:`target_of` the children's environment, recorded in and checked against the marker."""
    steps: list[Step] = field(default_factory=list)
    repo_id: int | None = None
    upload_run_id: str | None = None

    @contextlib.contextmanager
    def step(self, name: str) -> Iterator[dict[str, Any]]:
        """Time a step; it passes unless the body raises or sets ``row["ok"] = False``.

        The body fills ``row["detail"]`` (and may set ``row["warn"]``). An exception becomes a
        FAIL row with its message, and is re-raised as :class:`StepFailed` so later steps that
        need this one's result do not run against nothing.
        """
        row: dict[str, Any] = {"ok": True, "detail": "", "warn": False}
        started = time.monotonic()
        self.echo(f"... {name}")
        try:
            yield row
        except Exception as exc:
            self.steps.append(
                Step(name, False, _clip(f"{type(exc).__name__}: {exc}"), time.monotonic() - started)
            )
            raise StepFailed(name) from exc
        step = Step(name, bool(row["ok"]), row["detail"], time.monotonic() - started, bool(row["warn"]))
        self.steps.append(step)
        if not step.ok:
            raise StepFailed(name)

    @property
    def passed(self) -> bool:
        return all(step.ok for step in self.steps)


class StepFailed(Exception):
    """A step failed; the steps after it that depend on it are skipped."""


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def free_port() -> int:
    """A loopback TCP port nothing listens on right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def child_environment(
    environ: Mapping[str, str], *, port: int, admin_token: str, data_dir: Path | None = None
) -> dict[str, str]:
    """The environment of every child: the caller's state settings, plus a loopback server on
    ``port`` with ``admin_token``, the local executor and the fake forge (no sandbox, no GitHub).

    With ``data_dir`` (always, from :func:`main`) the data directory and the cache under it replace
    any from the caller: the process environment outranks ``.env``, so neither reaches the laptop's.
    """
    env = {key: value for key, value in environ.items() if not key.startswith(CHILD_ENV_DROP)}
    env.update(
        {
            "COVERSET_EXECUTOR": "local",
            "COVERSET_FORGE": "fake",
            "COVERSET_BIND": f"127.0.0.1:{port}",
            "COVERSET_PUBLIC_URL": f"http://127.0.0.1:{port}",
            "COVERSET_ADMIN_TOKEN": admin_token,
            "COVERSET_TRUST_PROXY": "false",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if data_dir is not None:
        env["COVERSET_DATA_DIR"] = str(data_dir)
        env["COVERSET_CACHE_DIR"] = str(data_dir / "cache")
    for key in ("COVERSET_GITHUB_APP_ID", "COVERSET_GITHUB_TOKEN", "COVERSET_DEV_LOGIN"):
        env.pop(key, None)
    return env


def settings_for(env: Mapping[str, str]) -> Settings:
    """Settings as the children see them (``env`` over the process environment and ``.env``)."""
    overrides = {
        "executor": env["COVERSET_EXECUTOR"],
        "forge": env["COVERSET_FORGE"],
        "bind": env["COVERSET_BIND"],
        "public_url": env["COVERSET_PUBLIC_URL"],
        "admin_token": env["COVERSET_ADMIN_TOKEN"],
        "trust_proxy": False,
    }
    if "COVERSET_DATA_DIR" in env:
        overrides["data_dir"] = Path(env["COVERSET_DATA_DIR"])
    for name in ("database_url", "blob_url", "cache_dir"):
        value = env.get(f"COVERSET_{name.upper()}")
        if value:
            overrides[name] = value
    return Settings(**overrides)


def target_of(settings: Settings) -> dict[str, str]:
    """The database and store a run talks to, as recorded in :data:`MARKER` (no password).

    From the settings, not the environment, so a URL from ``.env`` counts too; local files are named
    relative to the data directory, which they live in.
    """
    database = make_url(settings.database_url).render_as_string(hide_password=True)
    return {"database": database, "store": settings.blob_url or "files in the data dir"}


def default_data_dir(target: Mapping[str, str]) -> Path:
    """``data/smoke-hosted/<12 hex digits>``: fixed for one database and bucket, apart for others."""
    digest = hashlib.sha256(json.dumps(dict(target), sort_keys=True).encode()).hexdigest()
    return SMOKE_DATA_ROOT / digest[:12]


def describe_state(settings: Settings) -> Step:
    """The ``state`` row: what this run talks to (a warning when it is not the hosted shape)."""
    database = make_url(settings.database_url).render_as_string(hide_password=True)
    store = settings.blob_url.split("?", 1)[0] if settings.blob_url else f"files under {settings.data_dir}"
    detail = f"database {database}; store {store}"
    if settings.hosted:
        return Step("state", True, f"hosted: {detail}")
    return Step("state", True, f"not hosted (local backends): {detail}", warn=True)


def coverset_command(*args: str) -> list[str]:
    """``coverset ARGS`` with this interpreter (no reliance on PATH)."""
    return [sys.executable, "-c", "from coverset.cli import main; main()", *args]


def run_child(
    argv: list[str], env: Mapping[str, str], *, cwd: Path | None = None, timeout: float = 600
) -> subprocess.CompletedProcess[str]:
    """Run a child to completion, capturing its output; raises with the output's tail on failure."""
    result = subprocess.run(  # noqa: S603 - our own interpreter and arguments
        argv, env=dict(env), cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        tail = (result.stdout + result.stderr)[-1500:]
        raise RuntimeError(
            f"{' '.join(argv[3:] if argv[1] == '-c' else argv)} exited {result.returncode}: {tail}"
        )
    return result


def fetch(url: str, token: str | None = None, *, timeout: float = 30) -> tuple[int, str]:
    """GET ``url`` (bearer ``token``) and return ``(status, text)``; never through a proxy."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_healthy(base: str, process: subprocess.Popen[Any], timeout: float = SERVE_TIMEOUT_S) -> float:
    """Seconds until ``base/healthz`` answered 200; raises when the process exits or time runs out."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if process.poll() is not None:
            raise RuntimeError(f"coverset serve exited with {process.returncode}")
        try:
            status, _ = fetch(f"{base}/healthz", timeout=2)
            if status == 200:
                return time.monotonic() - started
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"{base}/healthz did not answer within {timeout:.0f} s")


def run_async[T](factory: Callable[[], Any]) -> T:
    return asyncio.run(factory())


async def _with_db[T](settings: Settings, body: Callable[[Any], Any]) -> T:
    from coverset.db.session import Database

    database = Database.from_url(settings.database_url)
    try:
        async with database.session() as session:
            return await body(session)
    finally:
        await database.engine.dispose()


def find_repo_id(settings: Settings, full_name: str = DEMO_REPO) -> int | None:
    """The id of ``full_name`` in the database, or None."""
    from coverset.db.models import Repo

    async def body(session: Any) -> int | None:
        return await session.scalar(select(Repo.id).where(Repo.full_name == full_name).limit(1))

    return run_async(lambda: _with_db(settings, body))


def create_upload_token(settings: Settings, repo_id: int, name: str) -> str:
    """A repository token with the ``upload`` scope, inserted directly (as ``coverset users tokens
    create`` does); the raw value exists only in the return value."""
    from coverset.web import tokens

    async def body(session: Any) -> str:
        raw, _row = await tokens.create(
            session, name=name, kind="repo", scopes=["upload"], now=datetime.now(UTC), repo_id=repo_id
        )
        await session.commit()
        return raw

    return run_async(lambda: _with_db(settings, body))


def latest_upload_run(settings: Settings, repo_id: int, head_sha: str) -> str | None:
    """The newest upload-triggered run of ``head_sha`` in ``repo_id``."""
    from coverset.db.models import Run

    async def body(session: Any) -> str | None:
        query = (
            select(Run.id)
            .where(Run.repo_id == repo_id, Run.head_sha == head_sha, Run.trigger == "upload")
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        return await session.scalar(query)

    return run_async(lambda: _with_db(settings, body))


def run_rows(settings: Settings, run_id: str) -> dict[str, Any]:
    """The run's status and verdict and how many result and event rows it has."""
    from coverset.db.models import Run, RunEvent, TestResult

    async def body(session: Any) -> dict[str, Any]:
        run = await session.get(Run, run_id)
        if run is None:
            return {"run": None}
        results = await session.scalar(select(func.count()).where(TestResult.run_id == run_id))
        events = await session.scalar(select(func.count()).where(RunEvent.run_id == run_id))
        return {
            "run": run_id,
            "status": run.status,
            "verdict": run.verdict,
            "shards": run.shard_count,
            "test_results": int(results or 0),
            "events": int(events or 0),
        }

    return run_async(lambda: _with_db(settings, body))


def run_keys(settings: Settings, repo_id: int, run_id: str) -> list[str]:
    """The store's keys of one run's facts. With a blob URL this lists the bucket itself, so a run
    whose files only reached the local cache has no keys here."""
    from coverset.storage import from_settings

    prefix = f"facts/repo={repo_id}/run={run_id}/"
    return sorted(info.key for info in from_settings(settings).list(prefix))


def demo_pr(settings: Settings, number: int = PR_NUMBER) -> dict[str, Any]:
    """The demo PR's ``head_sha``, ``base_sha`` and ``branch`` from the seeded repository's manifest."""
    from coverset.demo.seed import read_shas

    shas = read_shas(settings.data_dir / "demo.git")
    if shas is None:
        raise RuntimeError(f"no demo repository under {settings.data_dir} (the demo step did not run)")
    return next(pr for pr in shas["prs"] if pr["number"] == number)


def existing_repo_id(settings: Settings) -> int | None:
    """The demo repository's id when the database already holds it (None for an empty database, one
    without coverset's tables, or one the server does not have yet, which this does not create)."""
    try:
        return find_repo_id(settings)
    except Exception:  # noqa: BLE001 - no schema yet: nothing of anyone's is in there
        return None


def read_marker(data_dir: Path) -> dict[str, Any] | None:
    """The data directory's :data:`MARKER`, or None when the smoke did not make it."""
    try:
        return json.loads((data_dir / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{data_dir / MARKER} is unreadable ({exc}); remove it by hand") from exc


def write_marker(data_dir: Path, marker: Mapping[str, Any]) -> None:
    (data_dir / MARKER).write_text(json.dumps(dict(marker), indent=2) + "\n", encoding="utf-8")


# -- the steps -------------------------------------------------------------------------------------


def step_data_dir(smoke: Smoke) -> None:
    """Claim the data directory (write :data:`MARKER`), or refuse one that is someone else's.

    A directory the smoke made before is reused while the database still holds the repository it
    recorded there. When the database no longer holds it (reset, dropped or purged elsewhere) the
    git repository and cache in the directory describe a database that is gone, and a new one
    numbers its repositories from 1 again, so they are cleared; a demo repository with another id
    is someone else's, and the smoke refuses it.
    """
    with smoke.step("data dir") as row:
        data_dir = smoke.settings.data_dir
        marker = read_marker(data_dir)
        if marker is not None:
            if marker.get("target") != smoke.target:
                raise RuntimeError(
                    f"{data_dir} belongs to a smoke of {marker.get('target')}, not {smoke.target}; "
                    "pass another --data-dir"
                )
            current, recorded = existing_repo_id(smoke.settings), marker.get("repo_id")
            if current is not None and recorded is not None and current != recorded:
                raise RuntimeError(
                    f"the database holds {DEMO_REPO} as repo {current}, but this smoke made repo "
                    f"{recorded}: that one is someone else's; use a database of its own"
                )
            row["detail"] = f"{data_dir} (the smoke's own, from {marker.get('created', 'an earlier run')})"
            if current is None and any(path.name != MARKER for path in data_dir.iterdir()):
                for path in data_dir.iterdir():
                    if path.name == MARKER:
                        continue
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                write_marker(data_dir, {key: value for key, value in marker.items() if key != "repo_id"})
                row["detail"] += (
                    f"; the database holds no {DEMO_REPO} any more, so its old cache and git repository went"
                )
            return
        if data_dir.exists() and any(data_dir.iterdir()):
            raise RuntimeError(
                f"{data_dir} holds files the smoke did not make (no {MARKER}); "
                "pass --data-dir with a new or empty directory"
            )
        repo_id = existing_repo_id(smoke.settings)
        if repo_id is not None:
            raise RuntimeError(
                f"the database already holds {DEMO_REPO} (repo {repo_id}), which this smoke did not "
                "make and would repoint at its own git repository; use a database of its own, or "
                f"`coverset repo purge {DEMO_REPO} --yes` there first"
            )
        data_dir.mkdir(parents=True, exist_ok=True)
        created = datetime.now(UTC).isoformat(timespec="seconds")
        write_marker(data_dir, {"target": smoke.target, "created": created})
        row["detail"] = f"{data_dir} (new; the cache is inside it)"


def step_migrate(smoke: Smoke) -> None:
    with smoke.step("migrate") as row:
        result = run_child(coverset_command("init-db"), smoke.env)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        applied = sum(line.startswith("applied migration") for line in lines)
        row["detail"] = f"{applied} migration(s) applied" if applied else "already at head"


def step_demo(smoke: Smoke) -> None:
    with smoke.step("demo") as row:
        run_child(
            coverset_command("demo", "--no-serve", "--no-report", "--executor", "local"),
            smoke.env,
            timeout=900,
        )
        smoke.repo_id = find_repo_id(smoke.settings)
        if smoke.repo_id is None:
            raise RuntimeError(f"{DEMO_REPO} is not in the database after the demo")
        marker = read_marker(smoke.settings.data_dir) or {"target": smoke.target}
        write_marker(smoke.settings.data_dir, {**marker, "repo_id": smoke.repo_id})
        row["detail"] = f"{DEMO_REPO} (repo {smoke.repo_id}): six baselines and two PRs run locally"


@contextlib.contextmanager
def served(smoke: Smoke) -> Iterator[str]:
    """``coverset serve`` on the configured port for the duration; yields the base URL."""
    base = smoke.env["COVERSET_PUBLIC_URL"]
    log_path = smoke.workdir / "serve.log"
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(  # noqa: S603 - our own interpreter and arguments
            coverset_command("serve"), env=smoke.env, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            with smoke.step("serve") as row:
                ready = wait_healthy(base, process)
                row["detail"] = f"{base} answered /healthz in {ready:.1f} s (log: {log_path})"
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def step_pages(smoke: Smoke, base: str) -> None:
    """Each page is its own row; a failing page does not stop the others or the upload."""
    rid = smoke.repo_id
    for name, path_template, marker in PAGES:
        path = path_template.format(rid=rid)
        with contextlib.suppress(StepFailed), smoke.step(f"page: {name}") as row:
            started = time.monotonic()
            status, text = fetch(base + path, smoke.admin_token)
            elapsed = time.monotonic() - started
            row["ok"] = status == 200 and marker.lower() in text.lower()
            found = "contains" if marker.lower() in text.lower() else "MISSING"
            row["detail"] = (
                f"GET {path}: HTTP {status}, {len(text) // 1024} KB, {found} {marker!r} ({elapsed:.2f} s)"
            )


def step_upload(smoke: Smoke, base: str) -> None:
    with smoke.step("upload") as row:
        assert smoke.repo_id is not None
        pr = demo_pr(smoke.settings)
        checkout = smoke.workdir / "checkout"
        shutil.rmtree(checkout, ignore_errors=True)
        demo_git = smoke.settings.data_dir / "demo.git"
        subprocess.run(
            ["git", "clone", "--quiet", str(demo_git), str(checkout)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", pr["head_sha"]],
            check=True,
            capture_output=True,
        )
        test_env = {k: v for k, v in smoke.env.items() if not k.startswith("COVERSET_")}
        run_child(
            [sys.executable, "-m", "pytest", "--coverset", "-q", "-p", "no:cacheprovider"],
            test_env,
            cwd=checkout,
        )
        label = "smoke-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        token = create_upload_token(smoke.settings, smoke.repo_id, label)
        upload = run_child(
            [
                sys.executable, "-m", "pytest_coverset.ci", "upload",
                "--server", base, "--repo", DEMO_REPO,
                "--sha", pr["head_sha"], "--base", pr["base_sha"], "--pr", str(PR_NUMBER),
                "--branch", pr["branch"], "--label", label,
                "--with-sources", "--wait", "--timeout", str(UPLOAD_TIMEOUT_S),
            ],
            {**test_env, "COVERSET_TOKEN": token},
            cwd=checkout,
            timeout=UPLOAD_TIMEOUT_S + 60,
        )  # fmt: skip
        output = upload.stdout + upload.stderr
        if token in output:
            raise RuntimeError("the repository token appeared in coverset-ci's output")
        smoke.upload_run_id = latest_upload_run(smoke.settings, smoke.repo_id, pr["head_sha"])
        verdict = next((line.strip() for line in output.splitlines() if "coverset verdict" in line), "")
        row["ok"] = smoke.upload_run_id is not None and "verdict" in verdict
        row["detail"] = (
            f"POST /v1/bundles label {label}: {verdict or 'no verdict line'}; run {smoke.upload_run_id}"
        )


def step_bucket(smoke: Smoke) -> None:
    with smoke.step("bucket") as row:
        assert smoke.repo_id is not None and smoke.upload_run_id is not None
        keys = run_keys(smoke.settings, smoke.repo_id, smoke.upload_run_id)
        where = smoke.settings.blob_url.split("?", 1)[0] or str(smoke.settings.data_dir)
        manifest = [key for key in keys if key.endswith("MANIFEST.json")]
        parquet = [key for key in keys if key.endswith(".parquet")]
        row["ok"] = bool(parquet)
        row["detail"] = (
            f"{len(keys)} keys under {where}/facts/repo={smoke.repo_id}/run={smoke.upload_run_id}/: "
            f"{len(parquet)} Parquet, {'a' if manifest else 'no'} MANIFEST.json"
            + (f" (e.g. {parquet[0].rsplit('/', 1)[-1]})" if parquet else "")
        )


def step_rows(smoke: Smoke) -> None:
    with smoke.step("rows") as row:
        assert smoke.upload_run_id is not None
        found = run_rows(smoke.settings, smoke.upload_run_id)
        row["ok"] = found.get("status") == "completed" and found.get("test_results", 0) > 0
        row["detail"] = (
            f"run {found.get('run')}: {found.get('status')} / {found.get('verdict')}, "
            f"{found.get('shards')} shard(s), {found.get('test_results')} test results, {found.get('events')} events"
        )


def clean(smoke: Smoke) -> None:
    """Delete what the smoke made: the demo repository's rows and files, keys left under its
    prefixes, and the smoke's data directory (its git repository, cache and local blobs).

    Only with the data directory's :data:`MARKER` for this target: without it the smoke made
    nothing here, and a ``coverset-demo/ledger`` in the database is someone else's (``make
    dev-hosted``'s, say) and stays.
    """
    with smoke.step("clean") as row:
        data_dir = smoke.settings.data_dir
        marker = read_marker(data_dir)
        if marker is None:
            row["detail"] = f"nothing to clean: {data_dir} has no {MARKER} (the smoke made nothing there)"
            return
        if marker.get("target") != smoke.target:
            raise RuntimeError(f"{data_dir} belongs to a smoke of {marker.get('target')}, not {smoke.target}")
        parts = []
        repo_id = smoke.repo_id
        if repo_id is None:
            try:
                repo_id = find_repo_id(smoke.settings)
            except Exception as exc:  # noqa: BLE001 - no schema: nothing of the smoke's is in there
                parts.append(f"no coverset tables to clean ({type(exc).__name__})")
        recorded = marker.get("repo_id")
        if repo_id is not None and recorded is not None and repo_id != recorded:
            parts.append(f"left {DEMO_REPO} (repo {repo_id}) alone: the smoke made repo {recorded}")
            repo_id = None
        if repo_id is not None:
            run_child(coverset_command("repo", "purge", DEMO_REPO, "--yes"), smoke.env)
            parts.append(f"purged {DEMO_REPO} (repo {repo_id})")
            from coverset.storage import from_settings

            store = from_settings(smoke.settings)
            left = sum(
                store.delete_prefix(prefix)
                for prefix in (f"facts/repo={repo_id}/", f"sources/{repo_id}/", f"uploads/{repo_id}/")
            )
            parts.append(f"{left} leftover key(s) deleted")
        elif not parts:
            parts.append(f"{DEMO_REPO} is not in the database")
        shutil.rmtree(data_dir, ignore_errors=True)
        parts.append(f"removed {data_dir}")
        row["detail"] = "; ".join(parts)


def run_smoke(smoke: Smoke, *, clean_after: bool = False, clean_only: bool = False) -> list[Step]:
    """Run the steps in order (a failed step skips the ones that need it); returns the rows."""
    smoke.steps.append(describe_state(smoke.settings))
    with contextlib.suppress(StepFailed):
        if not clean_only:
            step_data_dir(smoke)
            step_migrate(smoke)
            step_demo(smoke)
            with served(smoke) as base:
                step_pages(smoke, base)
                step_upload(smoke, base)
            with contextlib.suppress(StepFailed):  # the rows are worth reading either way
                step_bucket(smoke)
            step_rows(smoke)
    if clean_after or clean_only:
        with contextlib.suppress(StepFailed):
            clean(smoke)
    return smoke.steps


def format_table(steps: list[Step]) -> str:
    """The report: one ``PASS``/``WARN``/``FAIL`` line per step, then a summary line."""
    width = max(len(step.name) for step in steps)
    lines = [f"{step.mark}  {step.name:<{width}}  {step.seconds:6.1f} s  {step.detail}" for step in steps]
    failed = sum(not step.ok for step in steps)
    lines.append(f"\n{len(steps) - failed} of {len(steps)} passed" + (f", {failed} FAILED" if failed else ""))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clean", action="store_true", help="Delete what the smoke made once it has run.")
    parser.add_argument("--clean-only", action="store_true", help="Only delete what an earlier smoke made.")
    parser.add_argument("--port", type=int, default=0, help="Port for the server (default: a free one).")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="The smoke's own data dir and cache (default: data/smoke-hosted/<digest of the target>; "
        "never the laptop's data/).",
    )
    parser.add_argument(
        "--workdir", type=Path, default=None, help="Scratch directory (default: a new temp dir)."
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the rows as JSON lines instead of a table."
    )
    return parser


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Run the smoke against the configured state; exit 0 when every row passed."""
    args = build_parser().parse_args(argv)
    source = dict(os.environ if environ is None else environ)
    admin_token = source.get("COVERSET_ADMIN_TOKEN") or secrets.token_hex(32)
    port = args.port or free_port()
    if args.data_dir is None:  # the target decides the default (the database and the store)
        probe = settings_for(
            child_environment(source, port=port, admin_token=admin_token, data_dir=SMOKE_DATA_ROOT)
        )
        data_dir = default_data_dir(target_of(probe))
    else:
        data_dir = args.data_dir
    env = child_environment(source, port=port, admin_token=admin_token, data_dir=data_dir.absolute())
    settings = settings_for(env)
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="coverset-smoke-"))
    workdir.mkdir(parents=True, exist_ok=True)
    smoke = Smoke(
        settings,
        env,
        workdir,
        admin_token,
        echo=lambda line: print(line, file=sys.stderr),
        target=target_of(settings),
    )
    steps = run_smoke(smoke, clean_after=args.clean, clean_only=args.clean_only)
    if args.json:
        for step in steps:
            print(json.dumps({**asdict(step), "mark": step.mark}, sort_keys=True))
    else:
        print(format_table(steps))
    if args.clean or args.clean_only:
        shutil.rmtree(workdir, ignore_errors=True)
    elif smoke.passed:
        print(
            f"\nthe data is still there: open the UI with the admin token, or rerun with --clean-only"
            f" (data dir: {settings.data_dir}; scratch: {workdir})"
        )
    return 0 if smoke.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
