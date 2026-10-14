"""The agent tools over a local ``.coverset`` directory: every tool, caps, pages, redaction, errors.

The bundle is the demo app's own ``pytest --coverset`` run (``conftest.demo_app``), so the numbers
are the ones the README quotes for ``validate_email``, ``parse_duration`` and ``allocate_cents``.
The facts-store backend answers the same tools through the HTTP API (``tests/web/test_agent_api.py``).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agent_helpers import RAW_EMAIL, DemoApp, assert_plain_and_capped, example_strings

from coverset.agent import cover
from coverset.agent.backends import LocalBackend, shipped_functions
from coverset.agent.explain import explain_shape_key, fast_check_hint
from coverset.agent.service import AgentService, scrub_text
from coverset.agent.tools import MAX_ROWS, TOOLS, ToolError, validate_args
from coverset.lang.base import FunctionRegion

ROOT = Path(__file__).resolve().parents[2]
PARSE = "src/ledger/durations/parse.py"


@pytest.fixture
def service(demo_app: DemoApp) -> AgentService:
    return AgentService(LocalBackend(demo_app.bundle, repo_root=demo_app.root))


async def call(service: AgentService, name: str, **args: Any) -> dict[str, Any]:
    result = await service.call(name, args)
    assert_plain_and_capped(result)
    return result


# -- every tool --------------------------------------------------------------------------------------

EVERY_TOOL: dict[str, dict[str, Any]] = {
    "list_repos": {},
    "list_runs": {},
    "get_run_summary": {},
    "get_patch_coverage": {"base": "main"},
    "get_function": {"function": "validate_email"},
    "find_gaps": {},
    "get_covering_tests": {"path": PARSE, "line": 18},
    "get_test": {"nodeid": "tests/test_parse.py::test_parse_duration[90s-90]"},
    "example_inputs": {"function": "validate_email"},
    "suggest_tests": {"function": "allocate_cents"},
    "minimal_test_set": {},
    "redundant_tests": {},
    "test_tax": {},
    "search": {"query": "parse"},
    "explain_shape_key": {"key": "str|ascii|kind:email/plain|len:11-100"},
}


def test_every_tool_is_exercised_here() -> None:
    assert set(EVERY_TOOL) == set(TOOLS)


@pytest.mark.parametrize("name", sorted(EVERY_TOOL))
async def test_every_tool_answers_plain_capped_json(service: AgentService, name: str) -> None:
    result = await call(service, name, **EVERY_TOOL[name])
    assert result
    if TOOLS[name].run_scoped:
        assert result["run"] == "local"


# -- repositories, runs, summary ---------------------------------------------------------------------


async def test_a_local_bundle_is_one_repository_with_one_run(
    service: AgentService, demo_app: DemoApp
) -> None:
    repos = await call(service, "list_repos")
    assert [r["name"] for r in repos["repos"]] == ["demo-app"]
    runs = await call(service, "list_runs")
    assert [r["id"] for r in runs["runs"]] == ["local"]
    assert (await call(service, "list_runs", kind="pr"))["runs"] == []
    with pytest.raises(ToolError) as caught:
        await service.call("get_run_summary", {"run": "run_0123"})
    assert caught.value.code == "not_found"


async def test_run_summary(service: AgentService) -> None:
    summary = await call(service, "get_run_summary")
    assert summary["tests"]["total"] == 97
    assert summary["tests"]["outcomes"] == {"passed": 97}
    assert summary["run"] == "local" and summary["run_info"]["verdict"] == "success"
    coverage = summary["coverage"]
    assert 0 < coverage["covered"] <= coverage["statements"]
    assert coverage["line_pct"] == round(100 * coverage["covered"] / coverage["statements"], 1)
    classes = summary["input_classes"]
    assert 0 < classes["observed"] < classes["expected"]
    assert summary["patch"] is None
    assert any("get_patch_coverage with base=" in step for step in summary["next_steps"])
    assert any("sampled" in caveat for caveat in summary["caveats"])


# -- functions ---------------------------------------------------------------------------------------


async def test_get_function_validate_email(service: AgentService) -> None:
    found = await call(service, "get_function", function="validate_email")
    assert found["function"] == "src/ledger/accounts/emailcheck.py::validate_email"
    assert found["coverage"]["line_pct"] == 100.0 and found["coverage"]["missing_lines"] == []
    classes = {c["class"]: c for c in found["input_classes"]["classes"]}
    assert found["input_classes"]["expected"] == 10 and found["input_classes"]["observed"] == 8
    missing = {name for name, c in classes.items() if not c["observed"]}
    assert missing == {"email:multi-label-domain", "email:idn"}
    assert classes["email:multi-label-domain"]["strategy"].startswith("st.sampled_from")
    assert classes["email:plain"]["strategy"] is None and classes["email:plain"]["tests"]
    assert found["tests"]["total"] >= 10
    assert found["tests"]["direct"] >= 1
    assert len(found["tests"]["rows"]) == min(20, found["tests"]["total"])
    few = await call(service, "get_function", function="validate_email", limit=2)
    assert len(few["tests"]["rows"]) == 2 and few["tests"]["truncated"] is True
    # signup reaches validate_email from in-repo code: an indirect caller
    assert any(c["function"].endswith("::check_if_new_user_signup_allowed") for c in found["callers"])


@pytest.mark.parametrize(
    "spec",
    [
        "parse_duration",
        f"{PARSE}::parse_duration",
        f"{PARSE}:16",
        "durations/parse.py:16",
    ],
)
async def test_function_names_resolve(service: AgentService, spec: str) -> None:
    found = await call(service, "get_function", function=spec)
    assert found["function"] == f"{PARSE}::parse_duration"
    by_key = await call(service, "get_function", function=found["func_key"])
    assert by_key["function"] == found["function"]


async def test_unknown_and_ambiguous_function_names(service: AgentService) -> None:
    with pytest.raises(ToolError) as caught:
        await service.call("get_function", {"function": "parse_dur"})
    assert caught.value.code == "not_found"
    assert f"{PARSE}::parse_duration" in caught.value.details["similar"]
    # an exact qualname wins over a method of the same bare name
    tax = await call(service, "get_function", function="tax_cents")
    method = await call(service, "get_function", function="Invoice.tax_cents")
    assert tax["qualname"] == "tax_cents" and method["qualname"] == "Invoice.tax_cents"


def test_a_bare_method_name_on_two_classes_is_ambiguous() -> None:
    def region(qualname: str, line: int) -> FunctionRegion:
        return FunctionRegion(
            f"k{line:015d}", "a.py", qualname, 0, line, line + 1, line + 2, "", "", (), "method"
        )

    ctx = SimpleNamespace(
        callables=[region("A.run", 1), region("B.run", 10)], by_key={}, facts=SimpleNamespace(statics={})
    )
    with pytest.raises(ToolError) as caught:
        AgentService(None).resolve(ctx, "run")  # type: ignore[arg-type]
    assert caught.value.code == "ambiguous"
    assert [c["function"] for c in caught.value.details["candidates"]] == ["a.py::A.run", "a.py::B.run"]
    assert AgentService(None).resolve(ctx, "a.py::B.run").qualname == "B.run"  # type: ignore[arg-type]


def test_path_specs_match_whole_components_and_never_pick_silently() -> None:
    def region(path: str, qualname: str) -> FunctionRegion:
        return FunctionRegion(
            f"{abs(hash(path)) % 10**15:015d}x", path, qualname, 0, 1, 2, 5, "", "", (), "function"
        )

    a, b, data = region("pkg_a/utils.py", "f"), region("pkg_b/utils.py", "f"), region("pkg/data.py", "g")
    regions = [a, b, data]
    ctx = SimpleNamespace(
        callables=regions,
        by_key={},
        facts=SimpleNamespace(statics={r.path: None for r in regions}),
        innermost=lambda path, line: next((r for r in regions if r.path == path), None),
    )
    resolve = AgentService(None).resolve  # type: ignore[arg-type]
    assert resolve(ctx, "pkg_b/utils.py:3") is b  # type: ignore[arg-type]
    assert resolve(ctx, "./pkg_a/utils.py:3") is a  # type: ignore[arg-type]
    with pytest.raises(ToolError) as caught:
        resolve(ctx, "utils.py:3")  # type: ignore[arg-type]
    assert caught.value.code == "ambiguous"
    assert caught.value.details["candidates"] == ["pkg_a/utils.py", "pkg_b/utils.py"]
    with pytest.raises(ToolError) as caught:
        resolve(ctx, "ils.py:3")  # type: ignore[arg-type]
    assert caught.value.code == "not_found", "a suffix inside a file name is not a match"
    assert resolve(ctx, "data.py::g") is data  # type: ignore[arg-type]
    with pytest.raises(ToolError):
        resolve(ctx, "ta.py::g")  # type: ignore[arg-type]


async def test_partial_file_names_are_not_matches(service: AgentService) -> None:
    for name, args in (
        ("get_function", {"function": "rse.py:16"}),
        ("get_covering_tests", {"path": "rse.py", "line": 18}),
    ):
        with pytest.raises(ToolError) as caught:
            await service.call(name, args)
        assert caught.value.code == "not_found"
        assert PARSE in caught.value.details["similar"]


async def test_find_gaps_sorts_pages_and_filters(service: AgentService) -> None:
    first = await call(service, "find_gaps", limit=3)
    rows = first["gaps"]
    assert len(rows) == 3 and first["total"] > 3 and first["next_cursor"] == "3"
    assert [r["missing_count"] for r in rows] == sorted((r["missing_count"] for r in rows), reverse=True)
    second = await call(service, "find_gaps", limit=3, cursor=first["next_cursor"])
    assert not {r["function"] for r in rows} & {r["function"] for r in second["gaps"]}
    every = await call(service, "find_gaps", limit=MAX_ROWS)
    assert every["total"] == first["total"] and every["next_cursor"] is None
    names = {r["qualname"] for r in every["gaps"]}
    assert {"validate_email", "parse_duration", "allocate_cents"} <= names
    allocate = next(r for r in every["gaps"] if r["qualname"] == "allocate_cents")
    assert "total_cents:negative" in allocate["missing"]
    assert allocate["sampled"] is True  # one test hit the shape tracer's sample cap
    only = await call(service, "find_gaps", path="src/ledger/billing", limit=MAX_ROWS)
    assert only["gaps"] and all(r["path"].startswith("src/ledger/billing") for r in only["gaps"])
    ratio = await call(service, "find_gaps", sort="ratio", limit=MAX_ROWS)
    pcts = [r["input_pct"] for r in ratio["gaps"]]
    assert pcts == sorted(pcts)


async def test_limits_are_capped_by_validation(service: AgentService) -> None:
    with pytest.raises(ToolError) as caught:
        await service.call("find_gaps", {"limit": MAX_ROWS + 1})
    assert caught.value.code == "invalid_argument"
    with pytest.raises(ToolError):
        await service.call("find_gaps", {"cursor": "not-a-cursor"})


# -- covering tests and one test -------------------------------------------------------------------


async def test_covering_tests_of_a_line_a_function_and_a_file(service: AgentService) -> None:
    line = await call(service, "get_covering_tests", path=PARSE, line=18, limit=5)
    assert line["total"] >= 5 and line["import_time"] is False
    durations = [t["duration_s"] for t in line["tests"]]
    assert durations == sorted(durations), "fastest first"
    assert line["pytest_command"].startswith("pytest ")
    function = await call(service, "get_covering_tests", function="parse_duration", limit=MAX_ROWS)
    whole_file = await call(service, "get_covering_tests", path=PARSE, limit=MAX_ROWS)
    assert {t["nodeid"] for t in line["tests"]} <= {t["nodeid"] for t in function["tests"]}
    assert {t["nodeid"] for t in function["tests"]} <= {t["nodeid"] for t in whole_file["tests"]}
    shorthand = await call(service, "get_covering_tests", path="durations/parse.py", line=18)
    assert shorthand["target"]["path"] == PARSE
    def_line = await call(service, "get_covering_tests", path=PARSE, line=9)
    assert def_line["import_time"] is True, "a def line runs at import"
    with pytest.raises(ToolError) as caught:
        await service.call("get_covering_tests", {"path": "nope.py", "line": 1})
    assert caught.value.code == "not_found"
    with pytest.raises(ToolError):
        await service.call("get_covering_tests", {})


async def test_get_test(service: AgentService) -> None:
    nodeid = "tests/test_parse.py::test_parse_duration[90s-90]"
    found = await call(service, "get_test", nodeid=nodeid)
    assert found["outcome"] == "passed" and found["file"] == "tests/test_parse.py"
    assert [f["path"] for f in found["files"]] == [PARSE]
    [function] = found["functions"]
    assert function["function"] == f"{PARSE}::parse_duration"
    assert 0 < function["statements_run"] <= function["body_statements"]
    shapes = {s["param"]: s for s in found["shapes"]}
    assert shapes["text"]["direct"] is True
    assert "text:single-unit" in shapes["text"]["classes"]
    assert shapes["text"]["example"] == "'90s'"
    assert found["history"] is None and "no history" in found["history_note"]
    with pytest.raises(ToolError) as caught:
        await service.call("get_test", {"nodeid": "tests/test_parse.py::test_nope"})
    assert caught.value.code == "not_found"


# -- examples, suggestions, redaction ---------------------------------------------------------------


async def test_example_inputs_are_redacted_shapes(service: AgentService) -> None:
    found = await call(service, "example_inputs", function="validate_email")
    assert "redacted shapes, not raw data" in found["how_to_use"]
    examples = example_strings(found)
    assert examples, "observed classes carry examples"
    assert any("***@" in text for text in examples), "email local parts are masked"
    for text in examples:
        assert not RAW_EMAIL.search(text), text
    classes = {c["class"]: c for c in found["classes"]}
    assert classes["email:idn"]["observed"] is False
    assert classes["email:idn"]["examples"] == [] and classes["email:idn"]["strategy"]
    plain = classes["email:plain"]["examples"]
    assert plain and all(e["test"] for e in plain)
    one = await call(service, "example_inputs", function="allocate_cents", param="total_cents")
    assert {c["param"] for c in one["classes"]} == {"total_cents"}
    returns = await call(service, "example_inputs", function="parse_duration", param="return")
    assert returns["classes"] == [] and returns["returns"]


async def test_no_raw_email_in_any_example_anywhere(service: AgentService) -> None:
    """Nodeids are as the runner wrote them (and the demo's contain addresses); every example value
    the tools return is redacted."""
    for name, args in (
        ("get_function", {"function": "validate_email"}),
        ("example_inputs", {"function": "validate_email", "limit": MAX_ROWS}),
        (
            "get_test",
            {
                "nodeid": "tests/test_emailcheck.py::test_validate_email_returns_the_domain[alice@example.com-example.com]"
            },
        ),
        ("get_function", {"function": "check_if_new_user_signup_allowed"}),
    ):
        result = await call(service, name, **args)
        for text in example_strings(result):
            assert not RAW_EMAIL.search(text), (name, text)


async def test_suggest_tests_for_allocate_cents(service: AgentService) -> None:
    found = await call(service, "suggest_tests", function="allocate_cents")
    assert found["framework"] == "hypothesis" and found["language"] == "python"
    missing = {m["class"]: m for m in found["missing"]}
    assert missing["total_cents:negative"]["hypothesis"] == "st.integers(max_value=-1)"
    assert "weights:sum-zero" in missing
    assert "@given(" in found["snippet"] and "st.integers(max_value=-1)" in found["snippet"]
    assert "from ledger.billing.allocate import allocate_cents" in found["snippet"]
    assert any("not a bug report" in note for note in found["notes"])


def test_scrub_text_masks_emails_and_credentials() -> None:
    assert scrub_text("'bob@acme.com'") == "'b***@acme.com'"
    assert "ghp_" in scrub_text("token ghp_" + "a" * 36) and "a" * 36 not in scrub_text("ghp_" + "a" * 36)
    long = scrub_text("x" * 500)
    assert long is not None and len(long) == 200 and long.endswith("…")
    assert scrub_text(None) is None


# -- set cover ---------------------------------------------------------------------------------------


async def test_minimal_test_set_keeps_every_fact(service: AgentService) -> None:
    found = await call(service, "minimal_test_set", limit=MAX_ROWS)
    kept, dropped = found["kept"], found["dropped"]
    assert found["exhaustive"] is True
    assert kept["count"] + dropped["count"] == found["universe"]["tests_in_scope"]
    assert 0 < kept["count"] < 97 and dropped["saving_pct"] > 50
    assert all(row["loses"] == {"lines": 0, "arcs": 0, "classes": 0} for row in dropped["tests"])
    assert all(row["covered_by"] for row in dropped["tests"])
    # the property-based tests are kept by default
    kept_ids = {t["nodeid"] for t in kept["tests"]}
    assert any("test_properties.py" in n for n in kept_ids)
    assert not any(row["property_based"] for row in dropped["tests"])
    # recompute: the kept tests reach every fact of the universe
    ctx = await service.backend.context(None, None)
    index = cover.cover_index(ctx)
    position = {n: i for i, n in enumerate(index.nodeids)}
    kept_idx = {position[n] for n in kept_ids}
    assert all(set(holders) & kept_idx for holders in index.tests)
    page = await call(service, "minimal_test_set", limit=3)
    assert len(page["deselect_args"].splitlines()) == 3 and page["deselect_truncated"] is True
    assert page["deselect_count"] == dropped["count"]
    everything = await call(service, "minimal_test_set", limit=3, deselect="all")
    assert len(everything["deselect_args"].splitlines()) == dropped["count"]
    assert everything["deselect_truncated"] is False
    assert (await call(service, "minimal_test_set", limit=3, deselect="none"))["deselect_args"] == ""
    lines = found["deselect_args"].splitlines()
    assert len(lines) == dropped["count"] and all(line.startswith("--deselect=tests/") for line in lines)
    assert any("candidates, not verdicts" in c for c in found["caveats"])


async def test_minimal_test_set_without_property_tests_and_with_a_budget(service: AgentService) -> None:
    free = await call(service, "minimal_test_set", keep_property_tests=False, limit=MAX_ROWS)
    default = await call(service, "minimal_test_set", limit=MAX_ROWS)
    assert free["kept"]["duration_s"] <= default["kept"]["duration_s"]
    tight = await call(service, "minimal_test_set", budget_s=0.005, limit=MAX_ROWS, keep_property_tests=False)
    assert tight["exhaustive"] is False
    assert tight["kept"]["duration_s"] <= 0.005 + 1e-9
    assert any(sum(row["loses"].values()) for row in tight["dropped"]["tests"])
    lines_only = await call(service, "minimal_test_set", facts=["lines"], limit=MAX_ROWS)
    assert lines_only["universe"]["arcs"] == 0 and lines_only["universe"]["classes"] == 0
    assert lines_only["kept"]["count"] <= default["kept"]["count"]


async def test_minimal_test_set_by_path_and_by_change(service: AgentService) -> None:
    scoped = await call(service, "minimal_test_set", scope="path", path="src/ledger/billing/", limit=MAX_ROWS)
    assert scoped["universe"]["tests_in_scope"] < 97
    assert all("unique_facts_elsewhere" in row for row in scoped["dropped"]["tests"])
    with pytest.raises(ToolError):
        await service.call("minimal_test_set", {"scope": "path"})
    with pytest.raises(ToolError):
        await service.call("minimal_test_set", {"scope": "changed"})
    changed = await call(service, "minimal_test_set", scope="changed", base="main")
    # parse_minutes is new and untested: its lines are in no test's facts, so nothing is in scope
    assert changed["universe"]["lines"] == 0 and changed["kept"]["count"] == 0


def test_solve_covers_a_small_instance_and_the_budget_cuts_it() -> None:
    index = cover.CoverIndex(["a", "b", "c", "d"])
    index.add("lines", "x.py", 1, (0, 1))
    index.add("lines", "x.py", 2, (1,))
    index.add("lines", "x.py", 3, (2,))
    index.add("lines", "x.py", 4, (1, 2, 3))
    index.add("classes", "x.py", "n:zero", (3,), "f")
    weights = {0: 1.0, 1: 1.0, 2: 1.0, 3: 5.0}
    full = cover.solve(index, range(len(index)), weights)
    assert full.exhaustive
    covered = set().union(*(full.blocks.of_test[t] for t in full.kept))
    assert covered == set(range(len(full.blocks.tests)))
    assert sorted(full.kept) == [1, 2, 3]
    cut = cover.solve(index, range(len(index)), weights, budget_s=2.0)
    assert not cut.exhaustive and sum(weights[t] for t in cut.kept) <= 2.0


async def test_redundant_tests_from_the_economy(service: AgentService) -> None:
    found = await call(service, "redundant_tests", limit=MAX_ROWS)
    assert found["available"] and found["source"] == "test economy: computed from the local bundle"
    assert found["equivalent_groups"], "the demo has parametrized cases with identical facts"
    for group in found["equivalent_groups"]:
        assert group["keep"] in group["tests"] and group["size"] >= 2
    for row in found["dominated"]:
        assert row["test"] != row["dominated_by"]
    ctx = await service.backend.context(None, None)
    saved = ctx.cache.get("economy")
    ctx.cache["economy"] = (None, "switched off for this test")
    try:
        off = await call(service, "redundant_tests", limit=MAX_ROWS)
    finally:
        ctx.cache["economy"] = saved
    assert off["available"] is False and off["reason"] == "switched off for this test"
    assert off["equivalent_groups"] == [] and off["dominated"] == []


async def test_test_tax(service: AgentService) -> None:
    found = await call(service, "test_tax", limit=5)
    if not found["available"]:  # a build without the economy analysis says why
        assert found["reason"]
        return
    assert found["totals"]["tests"] == 97
    assert found["pr_tax"] is None, "a local bundle has no base run"
    leverage = [t["leverage"] for t in found["tests"] if t["leverage"] is not None]
    assert leverage == sorted(leverage)
    by_statements = await call(service, "test_tax", sort="statements", limit=5)
    counts = [t["statements"] for t in by_statements["tests"] if t["statements"] is not None]
    assert counts == sorted(counts, reverse=True)
    ctx = await service.backend.context(None, None)
    saved = ctx.cache.get("economy")
    ctx.cache["economy"] = (None, "not computed")
    try:
        off = await call(service, "test_tax")
    finally:
        ctx.cache["economy"] = saved
    assert off == {
        "run": "local",
        "repo": "demo-app",
        "available": False,
        "reason": "not computed",
        "pr_tax": None,
        "tests": [],
    }


# -- patch coverage on the work tree -----------------------------------------------------------------


async def test_patch_coverage_needs_a_base_locally(service: AgentService) -> None:
    found = await call(service, "get_patch_coverage")
    assert found["applicable"] is False and "base" in found["reason"]
    with pytest.raises(ToolError) as caught:
        await service.call("get_patch_coverage", {"base": "--upload-pack=evil"})
    assert caught.value.code == "invalid_argument"


async def test_patch_coverage_against_main(service: AgentService) -> None:
    found = await call(service, "get_patch_coverage", base="main")
    assert found["applicable"] is True and found["stale"] == []
    assert found["zero_test_functions"] == [f"{PARSE}::parse_minutes"]
    [file] = found["files"]
    assert file["path"] == PARSE and file["covered"] < file["total"] and file["missing_lines"]
    assert found["patch_pct"] is not None and found["patch_pct"] < 100
    [row] = found["functions"]
    assert row["change"] == "added" and row["tests"] == 0
    assert row["input_classes"]["observed"] == 0


async def test_patch_coverage_flags_files_edited_after_the_run(demo_app: DemoApp, tmp_path: Path) -> None:
    copy = tmp_path / "demo-app"
    shutil.copytree(demo_app.root, copy, symlinks=True)
    parse = copy / PARSE
    later = os.stat(copy / ".coverset" / "coverage.sqlite").st_mtime + 10
    parse.write_text(parse.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    os.utime(parse, (later, later))
    service = AgentService(LocalBackend(copy / ".coverset", repo_root=copy))
    found = await call(service, "get_patch_coverage", base="main")
    assert found["stale"] == [PARSE]
    assert any("pytest --coverset" in note for note in found["notes"])


async def test_the_bundle_is_reread_when_it_changes(demo_app: DemoApp, tmp_path: Path) -> None:
    copy = tmp_path / "bundle"
    shutil.copytree(demo_app.bundle, copy, symlinks=True)
    backend = LocalBackend(copy, repo_root=demo_app.root)
    first = await backend.context(None, None)
    assert await backend.context("latest", None) is first
    results = copy / "results.jsonl"
    stamp = results.stat().st_mtime + 5
    os.utime(results, (stamp, stamp))
    assert await backend.context(None, None) is not first


# -- a TypeScript bundle -----------------------------------------------------------------------------


async def test_typescript_bundles_get_fast_check() -> None:
    fixture = ROOT / "fixtures" / "bundles" / "neutral-ts"
    service = AgentService(LocalBackend(fixture / "bundle", repo_root=fixture / "repo"))
    found = await call(service, "suggest_tests", function="rateFor")
    assert found["language"] == "typescript" and found["framework"] == "fast-check"
    missing = {m["class"]: m for m in found["missing"]}
    assert missing["zone:empty"]["fast_check"] == 'fc.constant("")'
    assert missing["weightsGrams:empty"]["fast_check"] == "fc.constant([])"
    assert 'import fc from "fast-check"' in found["snippet"] and "fc.assert(" in found["snippet"]


def test_fast_check_translations() -> None:
    assert fast_check_hint("total:negative", "st.integers(max_value=-1)") == "fc.integer({ max: -1 })"
    assert fast_check_hint("zone:'eu'", "st.just('eu')") == "fc.constant('eu')"
    assert fast_check_hint("flag:none", "st.none()") == "fc.constant(null)"
    assert fast_check_hint("xs:empty", None) is None
    assert (
        fast_check_hint("text:signed", 'st.sampled_from(["-5m", "+5m"])') == 'fc.constantFrom("-5m", "+5m")'
    )


# -- grammar and validation --------------------------------------------------------------------------


def test_explain_shape_key() -> None:
    found = explain_shape_key("list|dups|elem:int|len:2-10|order:sorted")
    assert found["valid"] and found["type"] == "list"
    assert "has a repeated element" in found["in_words"] and "length 2-10" in found["in_words"]
    email = explain_shape_key("str|ascii|kind:email/multi-label-domain|len:11-100")
    assert "three or more labels" in email["in_words"]
    bad = explain_shape_key("str|zzz")
    assert bad["valid"] is False and bad["problems"]
    assert explain_shape_key("|x")["valid"] is False


def test_validate_args() -> None:
    assert validate_args("find_gaps", {"limit": 5.0})["limit"] == 5
    assert validate_args("find_gaps", None)["limit"] == 20
    for bad in ({"limit": True}, {"limit": 0}, {"sort": "best"}, {"nope": 1}, {"path": 3}):
        with pytest.raises(ToolError) as caught:
            validate_args("find_gaps", bad)
        assert caught.value.code == "invalid_argument", bad
    with pytest.raises(ToolError) as caught:
        validate_args("get_function", {})
    assert "function" in caught.value.message
    with pytest.raises(ToolError) as caught:
        validate_args("drop_tables", {})
    assert caught.value.code == "unknown_tool" and caught.value.status == 404
    assert validate_args("minimal_test_set", {"facts": ["lines", "lines"]})["facts"] == ["lines"]


def test_errors_are_json() -> None:
    error = ToolError("ambiguous", "two of them", {"candidates": [{"function": "a"}]})
    assert json.loads(json.dumps(error.to_json())) == {
        "error": "ambiguous",
        "message": "two of them",
        "candidates": [{"function": "a"}],
    }
    assert error.status == 400


async def test_an_edited_work_tree_keeps_the_tested_regions(demo_app: DemoApp, tmp_path: Path) -> None:
    """After an edit that moves lines, functions keep the regions of the text the tests ran (the
    plugin's own function rows), not whatever the work tree holds now."""
    regions, _ = shipped_functions(demo_app.bundle)
    assert any(r.qualname == "parse_duration" and r.path == PARSE for r in regions)
    copy = tmp_path / "demo-app"
    shutil.copytree(demo_app.root, copy, symlinks=True)
    parse = copy / PARSE
    parse.write_text("# a new first line moves every function down\n" + parse.read_text(encoding="utf-8"))
    later = os.stat(copy / ".coverset" / "coverage.sqlite").st_mtime + 10
    os.utime(parse, (later, later))
    fresh = await call(
        AgentService(LocalBackend(demo_app.bundle, repo_root=demo_app.root)),
        "get_function",
        function="parse_duration",
    )
    edited = await call(
        AgentService(LocalBackend(copy / ".coverset", repo_root=copy)),
        "get_function",
        function="parse_duration",
    )
    assert edited["def_line"] == fresh["def_line"] and edited["end_line"] == fresh["end_line"]
    assert edited["coverage"] == fresh["coverage"]
    assert edited["input_classes"]["expected"] == fresh["input_classes"]["expected"]
