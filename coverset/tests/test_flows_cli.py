"""``coverset flows import|windows|gaps|archetypes|prune`` on the seeded demo, plus probe-to-CLI end to end.

The demo (history and both PRs, LocalExecutor, fake forge) is run once for the module, as in
``tests/orchestrator/test_commands_facts.py``; ``fixtures/flows/ledger-prod.jsonl`` (three windows
of synthetic production traffic through the demo app under the real probe, see
``fixtures/flows/generate.py``) is imported once. The demo's tests never pass ``validate_email`` an
address on a subdomain; production does (15 % of signups), so that class must come out as a gap.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from coverset.cli import cli
from coverset.config import Settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "flows" / "ledger-prod.jsonl"
REPO = "coverset-demo/ledger"


@dataclass(frozen=True)
class Seeded:
    data_dir: Path
    db_path: Path
    baseline_run: str


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Seeded:
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("the demo needs uv and git")
    from coverset.db.session import init_db
    from coverset.demo.flow import run_demo
    from coverset.forge.fake import FakeForge
    from coverset.forge.signing import TEST_WEBHOOK_SECRET
    from coverset.logging import clear_registered_secrets
    from coverset.orchestrator.bundles import IngestPool
    from coverset.orchestrator.runner import OrchestratorContext

    root = tmp_path_factory.mktemp("flows-cli-demo")
    settings = Settings.for_tests(
        data_dir=root / "data",
        database_url=f"sqlite+aiosqlite:///{root / 'coverset.db'}",
        github_webhook_secret=TEST_WEBHOOK_SECRET,
        local_allow_webhook_runs=True,
    )

    async def scenario() -> None:
        await init_db(settings)
        ctx = OrchestratorContext.from_settings(settings, ingest=IngestPool("thread", max_workers=4))
        ctx.forge = FakeForge(session_factory=ctx.db.sessionmaker)
        try:
            await run_demo(ctx, out=root / "data" / "demo.git")
        finally:
            await ctx.close()
            assert ctx.ingest is not None
            ctx.ingest.close()

    asyncio.run(scenario())
    clear_registered_secrets()
    con = sqlite3.connect(root / "coverset.db")
    try:
        baseline = con.execute(
            "SELECT id FROM runs WHERE pr_id IS NULL AND status = 'completed' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        con.close()
    return Seeded(root / "data", root / "coverset.db", baseline)


@pytest.fixture(scope="module")
def env(seeded: Seeded) -> Iterator[dict[str, str]]:
    values = {
        "COVERSET_DATA_DIR": str(seeded.data_dir),
        "COVERSET_DATABASE_URL": f"sqlite+aiosqlite:///{seeded.db_path}",
    }
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    from coverset.config import get_settings

    get_settings.cache_clear()
    yield values
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def imported(env: dict[str, str]) -> dict[str, Any]:
    """The fixture file imported once (the first import's JSON report)."""
    return json.loads(
        _ok(CliRunner().invoke(cli, ["flows", "import", str(FIXTURE), "--repo", REPO, "--json"]))
    )


def _ok(result: Result) -> str:
    assert result.exit_code == 0, result.output
    return result.output


def _run(*args: str) -> str:
    return _ok(CliRunner().invoke(cli, ["flows", *args]))


def test_import_reports_and_is_idempotent(imported: dict[str, Any], seeded: Seeded) -> None:
    assert imported["windows"] == 3 and imported["rejected"] == []
    assert imported["services"] == ["ledger-api"]
    assert imported["reference_run"] == seeded.baseline_run
    again = _run("import", str(FIXTURE), "--repo", REPO)
    assert "imported 0 window(s)" in again and "skipped 3 window(s) already imported" in again
    windows = _run("windows", "--repo", REPO, "--json").splitlines()
    assert [json.loads(w)["service"] for w in windows] == ["ledger-api"] * 3


def test_gaps_list_the_subdomain_signups_production_sends(imported: dict[str, Any], seeded: Seeded) -> None:
    report = json.loads(_run("gaps", "--repo", REPO, "--windows", "3", "--json"))
    assert report["run"] == seeded.baseline_run
    by_name = {f["qualname"]: f for f in report["functions"]}
    email = {g["class"]: g for g in by_name["validate_email"]["gaps"]}
    gap = email["email:multi-label-domain"]
    assert 0.10 < gap["share"] < 0.20  # 15 % of signups in the generator's mix
    assert gap["calls"] > 50 and gap["traces"] >= 5
    assert "eng.acme.com" in gap["strategy"]
    assert gap["shapes"] == ["str|ascii|kind:email/multi-label-domain|len:11-100"]
    assert "email:plain" not in email and "email:plus-tag" not in email  # the tests pass those in
    duration = {g["class"] for g in by_name["parse_duration"]["gaps"]}
    assert {"text:multi-letter-unit", "text:decimal"} <= duration
    assert "text:single-unit" not in duration
    assert [f["function"] for f in report["never_tested"]] == ["src/ledger/util/text.py:initials"]
    assert report["flows_through_untested"][0]["untested"] == ["initials"]
    assert report["unresolved"] == []  # the lambda inside allocate_cents is counted, not listed
    assert report["unresolved_lambdas"] == 1


def test_gaps_text_names_the_class_and_the_strategy(imported: dict[str, Any]) -> None:
    text = _run("gaps", "--repo", REPO, "--windows", "3", "--limit", "10")
    assert "validate_email  src/ledger/accounts/emailcheck.py:8" in text
    line = next(line for line in text.splitlines() if "email:multi-label-domain" in line)
    assert "st.sampled_from" in line
    assert "called in production, never by a test:" in text


def test_one_window_is_the_default(imported: dict[str, Any]) -> None:
    report = json.loads(_run("gaps", "--repo", REPO, "--json"))
    assert "1 window(s)" in report["windows"]


def test_archetypes_name_the_untested_one(imported: dict[str, Any]) -> None:
    report = json.loads(
        _run("archetypes", "--repo", REPO, "--windows", "3", "--func", "validate_email", "--json")
    )
    [func] = report["functions"]
    assert func["common"] == ["email:ascii", "email:str"]
    by_name = {a["name"]: a for a in func["archetypes"]}
    assert by_name["email:plain"]["status"] == "exact"
    assert by_name["email:plain"]["share"] > 0.5
    subdomain = by_name["email:multi-label-domain"]
    assert subdomain["status"] == "untested" and subdomain["tests"] == []
    assert subdomain["strategy"].startswith("@given(email=st.sampled_from(")
    assert func["untested_share"] == pytest.approx(subdomain["share"])
    text = _run("archetypes", "--repo", REPO, "--windows", "3", "--patterns")
    assert "every call: email:ascii + email:str" in text and "frequent patterns:" in text


def test_prune_dry_run_keeps_everything(imported: dict[str, Any]) -> None:
    out = _run("prune", "--repo", REPO, "--older-than-days", "0", "--dry-run")
    assert out.startswith("would delete 1 batch(es)")
    assert len(_run("windows", "--repo", REPO, "--json").splitlines()) == 3


def test_errors_are_reported_cleanly(env: dict[str, str], tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"version": 9}\n')
    result = CliRunner().invoke(cli, ["flows", "import", str(bad), "--repo", REPO])
    assert result.exit_code == 1 and "rejected line 1" in result.output
    result = CliRunner().invoke(cli, ["flows", "gaps", "--repo", REPO, "--run", "run_nope"])
    assert result.exit_code != 0 and "no run 'run_nope'" in result.output
    result = CliRunner().invoke(cli, ["flows", "gaps", "--repo", REPO, "--service", "ghost"])
    assert result.exit_code != 0 and "no production windows" in result.output


PROBE_SCRIPT = """
import sys
import coverset_probe
from coverset_probe import JsonlExporter

coverset_probe.start(modules=["ledger"], service="probe-e2e", sample_rate=1.0, k_anonymity=3,
                     window_s=0, export=JsonlExporter(sys.argv[1]), source_root=sys.argv[2])
from ledger.accounts.emailcheck import validate_email
for i, email in enumerate(["ann@acme.com", "bob@eng.acme.com", "cy@ops.acme.org", "di@mail.eu.example.org"] * 3):
    with coverset_probe.trace(subject=f"user-{i}"):
        try:
            validate_email(email)
        except ValueError:
            pass
coverset_probe.stop()
"""


def test_a_fresh_probe_export_flows_through_import_and_gaps(imported: dict[str, Any], tmp_path: Path) -> None:
    out = tmp_path / "e2e.jsonl"
    path = os.pathsep.join(
        [str(ROOT / "packages" / "coverset-probe" / "src"), str(ROOT / "examples" / "demo-app" / "src")]
    )
    proc = subprocess.run(
        [sys.executable, "-c", PROBE_SCRIPT, str(out), str(ROOT / "examples" / "demo-app")],
        env={**os.environ, "PYTHONPATH": path},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(_run("import", str(out), "--repo", REPO, "--json"))
    assert result["windows"] == 1 and result["unresolved_functions"] == 0
    report = json.loads(_run("gaps", "--repo", REPO, "--service", "probe-e2e", "--json"))
    [func] = report["functions"]
    assert func["qualname"] == "validate_email"
    gap = next(g for g in func["gaps"] if g["class"] == "email:multi-label-domain")
    assert gap["share"] == pytest.approx(0.75)  # three of every four addresses in the script
    assert "ann@acme.com" not in out.read_text() and "eng.acme.com" not in out.read_text()


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_probe_s_vendored_features_match_the_plugin() -> None:
    """The probe's copy of the feature code computes the plugin's keys (the two must never drift)."""
    from pytest_coverset import features as plugin

    vendored = _load(
        ROOT / "packages" / "coverset-probe" / "src" / "coverset_probe" / "_features.py", "_probe_features"
    )
    corpus: list[object] = [
        None, True, 0, -7, 2**40, 0.0, 1.5, float("nan"), float("-inf"), "", " ", "abc", "café", " x ",
        "42", "2026-10-13", "https://e.com/a", "bob@acme.com", "bob@eng.acme.com", "bob+x@acme.com",
        "Bob@Acme.COM", "bob@bücher.de", "a@b.co.uk", "5m", "1h30m", "100ms", "1.5h", "-5m", "5m!",
        "x" * 3000, b"", b"ab", [], [3, 1, 2], [1, 1], [0, 0], [(1, 2), (5, 1)], (1, "a"), {1, 2},
        frozenset(), {}, {"a": 1}, object(), Path("x"),
    ]  # fmt: skip
    for value in corpus:
        assert vendored.shape_key(value) == plugin.shape_key(value), value


# -- review fixes ------------------------------------------------------------------------------------


def test_minutes_pools_every_window_that_ended_in_the_span(imported: dict[str, Any]) -> None:
    report = json.loads(
        _run("gaps", "--repo", REPO, "--service", "ledger-api", "--minutes", "10000000", "--json")
    )
    assert "3 window(s)" in report["windows"]


def test_never_tested_comes_from_coverage_when_shapes_are_missing(
    imported: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.facts import flows

    # As if the tracer had recorded nothing for these functions: coverage still knows who ran them.
    monkeypatch.setattr(flows, "run_shapes_by_function", lambda *_: {})
    report = json.loads(_run("gaps", "--repo", REPO, "--windows", "3", "--json"))
    assert [f["function"] for f in report["never_tested"]] == ["src/ledger/util/text.py:initials"]


def test_a_reference_run_without_shapes_is_refused(
    imported: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.facts import flows

    monkeypatch.setattr(flows, "run_has_shapes", lambda *_: False)
    for command in ("gaps", "archetypes"):
        result = CliRunner().invoke(cli, ["flows", command, "--repo", REPO])
        assert result.exit_code != 0 and "recorded no input shapes" in result.output, result.output


def test_read_time_resolution_builds_one_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows imported before any run existed resolve at read time: one index, not one per function."""
    import time
    from types import SimpleNamespace

    from coverset.commands.flows import _Context
    from coverset.facts import flows
    from coverset.lang.base import FunctionRegion

    regions = [
        FunctionRegion(
            f"src/pkg/m{i}.py:f{j}:0",
            f"src/pkg/m{i}.py",
            f"f{j}",
            0,
            1 + 10 * j,
            2 + 10 * j,
            9 + 10 * j,
            "b",
            "s",
            (),
            "function",
        )
        for i in range(400)
        for j in range(50)
    ]  # 20,000 regions
    funcs = [
        {
            "func_key": None,
            "module": f"pkg.m{i}",
            "qualname": f"f{i % 50}",
            "path": None,
            "line": 1 + 10 * (i % 50),
            "calls": 10,
        }
        for i in range(400)
    ]
    shapes = [
        {**f, "k_ok": True, "param": "x", "shape_key": "int|pos", "calls": 1.0, "traces": 5}
        for f in funcs
        for _ in range(5)
    ]  # 2,000 rows of 400 unresolved functions
    built: list[int] = []
    original = flows.FunctionResolver.__init__

    def counting(self: flows.FunctionResolver, regions: Any) -> None:
        built.append(1)
        original(self, regions)

    monkeypatch.setattr(flows.FunctionResolver, "__init__", counting)
    prod = SimpleNamespace(shapes=shapes, calls=[], funcs=funcs, flows=[], windows=[])
    started = time.perf_counter()
    ctx = _Context(None, None, None, prod, regions)
    elapsed = time.perf_counter() - started
    assert len(built) == 1
    assert len(ctx.rows) == 400 and ctx.unresolved == set()
    assert sum(ctx.func_calls.values()) == 4000
    assert elapsed < 5.0, elapsed  # was about 15 s per 400 functions with an index per function
