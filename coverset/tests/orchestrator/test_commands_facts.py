"""``coverset facts tables|explain|test|line|export`` on the seeded demo (run once for the module,
like ``tests/web/conftest.py``'s ``demo`` fixture)."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from coverset.cli import cli
from coverset.config import Settings
from coverset.db.testing import PostgresTestDatabases, run_sql
from coverset.facts.schema import TABLES

REPO = "coverset-demo/ledger"


@dataclass(frozen=True)
class SeededDemo:
    data_dir: Path
    db_url: str
    pr1_run: str
    baseline_run: str
    failing: str


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory, postgres_databases: PostgresTestDatabases) -> SeededDemo:
    """The local demo (history and both PRs, LocalExecutor, fake forge) in a temp directory."""
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("the demo needs uv and git")
    from coverset.db.session import init_db
    from coverset.demo.flow import run_demo
    from coverset.forge.fake import FakeForge
    from coverset.forge.signing import TEST_WEBHOOK_SECRET
    from coverset.logging import clear_registered_secrets
    from coverset.orchestrator.bundles import IngestPool
    from coverset.orchestrator.runner import OrchestratorContext

    root = tmp_path_factory.mktemp("facts-cli-demo")
    data_dir = root / "data"
    db_url = postgres_databases.create()
    settings = Settings.for_tests(
        data_dir=data_dir,
        database_url=db_url,
        github_webhook_secret=TEST_WEBHOOK_SECRET,
        local_allow_webhook_runs=True,
    )

    async def scenario() -> None:
        await init_db(settings)
        ctx = OrchestratorContext.from_settings(settings, ingest=IngestPool("thread", max_workers=4))
        ctx.forge = FakeForge(session_factory=ctx.db.sessionmaker)
        try:
            await run_demo(ctx, out=data_dir / "demo.git")
        finally:
            await ctx.close()
            assert ctx.ingest is not None
            ctx.ingest.close()

    asyncio.run(scenario())
    clear_registered_secrets()
    pr1 = run_sql(
        db_url,
        "SELECT r.id FROM runs r JOIN pull_requests p ON p.id = r.pr_id WHERE p.number = 1 "
        "ORDER BY r.created_at DESC LIMIT 1",
    )[0][0]
    baseline = run_sql(
        db_url,
        "SELECT id FROM runs WHERE pr_id IS NULL AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
    )[0][0]
    failing = run_sql(
        db_url,
        "SELECT t.nodeid FROM test_results r JOIN tests t ON t.id = r.test_id "
        "WHERE r.run_id = :run AND r.outcome = 'failed' ORDER BY r.rank LIMIT 1",
        {"run": pr1},
    )[0][0]
    return SeededDemo(data_dir, db_url, pr1, baseline, failing)


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, seeded: SeededDemo) -> Iterator[CliRunner]:
    monkeypatch.setenv("COVERSET_DATA_DIR", str(seeded.data_dir))
    monkeypatch.setenv("COVERSET_DATABASE_URL", seeded.db_url)
    from coverset.config import get_settings

    get_settings.cache_clear()
    yield CliRunner()
    get_settings.cache_clear()


def _ok(result: Result) -> str:
    assert result.exit_code == 0, result.output
    return result.output


def _json(runner: CliRunner, *args: str) -> Any:
    return json.loads(_ok(runner.invoke(cli, ["facts", *args, "--json"])))


def test_tables(runner: CliRunner, seeded: SeededDemo) -> None:
    data = _json(runner, "tables", "--repo", REPO, "--run", seeded.pr1_run)
    by_table = {row["table"]: row for row in data["tables"]}
    assert list(by_table) == list(TABLES)
    assert by_table["tests_dim"]["files"] == 3 and by_table["tests_dim"]["rows"] > 100
    assert by_table["ingest_log"]["rows"] == 3 and by_table["runs_dim"]["rows"] == 1
    assert data["bytes"] == sum(row["bytes"] for row in data["tables"]) > 0
    text = _ok(runner.invoke(cli, ["facts", "tables", "--repo", REPO]))  # default run: the baseline
    assert seeded.baseline_run in text and "test_lines" in text and "total " in text


def test_explain(runner: CliRunner, seeded: SeededDemo) -> None:
    data = _json(runner, "explain", "--repo", REPO, "--run", seeded.pr1_run)
    assert (
        data["run"]["id"] == seeded.pr1_run and data["run"]["pr"] == 1 and data["run"]["verdict"] == "failure"
    )
    assert data["coverage_complete"] and data["finalized"] and data["ingested_shards"] == 3
    assert [r["shard"] for r in data["ingest_log"]] == [0, 1, 2]
    assert all(r["committed"] and r["status"] == "ok" for r in data["ingest_log"])
    assert data["bundle_bytes"] > 0 and len(data["shards"]) == 3
    assert "first_failure" in data["timeline_s"] and data["timeline_s"]["first_failure"] > 0
    assert list(data["timeline_s"].values()) == sorted(data["timeline_s"].values())
    assert data["prior_run"] is not None and data["prior_run"]["status"] == "completed"
    assert set(data["limits"]) == {"dropped", "functions_sample_capped", "tests_over_sample_cap"}
    text = _ok(runner.invoke(cli, ["facts", "explain", "--repo", REPO, "--run", seeded.pr1_run]))
    for part in (
        "timeline (seconds from",
        "shards:",
        "ingest log:",
        "coverage_complete True",
        "scored against:",
    ):
        assert part in text
    missing = runner.invoke(cli, ["facts", "explain", "--repo", REPO, "--run", "run_nope"])
    assert missing.exit_code != 0 and "has no run" in missing.output


def test_test(runner: CliRunner, seeded: SeededDemo) -> None:
    data = _json(runner, "test", seeded.failing, "--repo", REPO, "--run", seeded.pr1_run)
    assert data["result"]["outcome"] == "failed" and data["result"]["rank"] == 1
    called = {fn["qualname"] for f in data["files"] for fn in f["functions"]}
    assert "refund" in called
    for f in data["files"]:
        for fn in f["functions"]:
            assert fn["lines"] and len(fn["lines"]) <= fn["body_statements"]
    assert data["shapes"] and any(s["function"] == "refund" for s in data["shapes"])
    assert any(h["run_id"] == seeded.pr1_run and h["outcome"] == "failed" for h in data["history"])
    text = _ok(runner.invoke(cli, ["facts", "test", seeded.failing, "--run", seeded.pr1_run]))
    assert "touched:" in text and "refund (:" in text and "passed in" in text and "history" in text
    unknown = runner.invoke(cli, ["facts", "test", "tests/nope.py::test_nope", "--run", seeded.pr1_run])
    assert unknown.exit_code != 0 and "no facts mention" in unknown.output


def test_line(runner: CliRunner, seeded: SeededDemo) -> None:
    touched = _json(runner, "test", seeded.failing, "--run", seeded.pr1_run)
    refund_file = next(
        f for f in touched["files"] if any(fn["qualname"] == "refund" for fn in f["functions"])
    )
    fn = next(fn for fn in refund_file["functions"] if fn["qualname"] == "refund")
    location = f"{refund_file['path']}:{fn['lines'][0]}"
    data = _json(runner, "line", location, "--run", seeded.pr1_run)
    assert data["function"]["qualname"] == "refund" and data["function"]["in_body"]
    nodeids = [t["nodeid"] for t in data["tests"]]
    assert nodeids[0] == seeded.failing  # run order: the failing test ran first
    ranks = [t["rank"] for t in data["tests"] if t.get("rank")]
    assert ranks == sorted(ranks)
    text = _ok(runner.invoke(cli, ["facts", "line", location, "--run", seeded.pr1_run]))
    assert "in refund (" in text and seeded.failing[:40] in text
    bad = runner.invoke(cli, ["facts", "line", "no-line-number", "--run", seeded.pr1_run])
    assert bad.exit_code == 2 and "PATH:LINE" in bad.output
    for odd in ("src/x.py:\u00b2", "src/x.py:\u0663"):  # a superscript two, an Arabic-Indic three
        superscript = runner.invoke(cli, ["facts", "line", odd, "--run", seeded.pr1_run])
        assert superscript.exit_code == 2 and "PATH:LINE" in superscript.output


def test_export(runner: CliRunner, seeded: SeededDemo, tmp_path: Path) -> None:
    target = tmp_path / "export"
    index = _json(runner, "export", "--repo", REPO, "--run", seeded.pr1_run, "--to", str(target))
    assert (target / "tables.json").is_file() and (target / "dictionary.json").is_file()
    assert json.loads((target / "tables.json").read_text()) == index
    inventory = {r["table"]: r["rows"] for r in _json(runner, "tables", "--run", seeded.pr1_run)["tables"]}
    for table in TABLES:
        entry = index["tables"][table]
        lines = (target / entry["file"]).read_text().splitlines()
        assert len(lines) == entry["rows"] == inventory[table], table
        if lines:
            assert set(json.loads(lines[0])) == set(entry["columns"])
    assert index["tables"]["ingest_log"]["source_files"] == [
        f"ingest_log.shard={n}.parquet" for n in range(3)
    ]


def test_export_refuses_foreign_dirs_and_never_follows_links(
    runner: CliRunner, seeded: SeededDemo, tmp_path: Path
) -> None:
    from coverset.commands.facts import EXPORT_MARKER

    target = tmp_path / "busy"
    target.mkdir()
    (target / "notes.txt").write_text("keep me", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("ORIGINAL", encoding="utf-8")
    (target / "files.jsonl").symlink_to(outside)
    refused = runner.invoke(cli, ["facts", "export", "--run", seeded.pr1_run, "--to", str(target)])
    assert refused.exit_code == 1 and "--force" in refused.output
    assert (target / "files.jsonl").is_symlink() and not (target / "tables.json").exists()
    forced = runner.invoke(cli, ["facts", "export", "--run", seeded.pr1_run, "--to", str(target), "--force"])
    assert forced.exit_code == 0, forced.output
    assert outside.read_text(encoding="utf-8") == "ORIGINAL"  # the link was replaced, not followed
    assert not (target / "files.jsonl").is_symlink() and (target / "files.jsonl").stat().st_size > 0
    assert (target / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (target / EXPORT_MARKER).is_file()
    # an earlier export is rewritten without --force, even with a planted link in it
    (target / "tables.json").unlink()
    (target / "tables.json").symlink_to(outside)
    again = runner.invoke(cli, ["facts", "export", "--run", seeded.pr1_run, "--to", str(target)])
    assert again.exit_code == 0, again.output
    assert outside.read_text(encoding="utf-8") == "ORIGINAL" and not (target / "tables.json").is_symlink()
    link = tmp_path / "link"
    link.symlink_to(target)
    assert runner.invoke(cli, ["facts", "export", "--run", seeded.pr1_run, "--to", str(link)]).exit_code == 1


def test_explain_notes_are_redacted_and_capped() -> None:
    from coverset.commands.facts import MAX_NOTE_CHARS, _safe_note

    token = "ghp_" + "a1B2" * 9
    note = _safe_note(f"error: bad row {token}\n\u202eevil " + "x" * 2000)
    assert token not in note and "\u202e" not in note and "\n" not in note
    assert "\\u202e" in note and len(note) == MAX_NOTE_CHARS and note.endswith("…")
