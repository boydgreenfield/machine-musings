"""End to end: a real Vitest run of ``examples/ts-app`` through ``coverset-vitest`` into facts and a report.

``npm test`` runs the example's suite with the reporter, the setup file and the instrumentation plugin
(``packages/coverset-vitest``) and writes a neutral bundle; nothing here is synthetic. The bundle must
validate with no problem, ingest as shard 0 of a TypeScript run exactly as a worker's tarball is
(:func:`~coverset.orchestrator.bundles.ingest_one`, regions and types from ``functions.jsonl``), answer
the facts queries, and build a static report whose function pages show the declared types.

Skipped (with the reason) when ``node`` or ``npm`` is missing or the example's dependencies are not
installed (``cd examples/ts-app && npm install``, after ``npm run build`` in the package).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coverset.facts import queries
from coverset.facts.ingest import RunContext, finalize_run
from coverset.facts.untrusted import BundleLimits
from coverset.lang import read_any_bundle
from coverset.lang.neutral import NeutralAdapter
from coverset.orchestrator.bundles import IngestJob, ingest_one
from coverset.storage.testing import local_blob_url, local_facts_store
from coverset_model.catalogue import NEVER_EXERCISED, function_catalogue_from_types
from coverset_model.io import pack, validate

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "examples" / "ts-app"
PREFIX = "examples/ts-app"
PRICING, EMAIL, SHIPPING = f"{PREFIX}/src/pricing.ts", f"{PREFIX}/src/email.ts", f"{PREFIX}/src/shipping.ts"
PRICING_TESTS = f"{PREFIX}/tests/pricing.test.ts"
EMAIL_TESTS = f"{PREFIX}/tests/email.test.ts"
REPO, RUN = 11, "vitest-run-1"
NPM_TIMEOUT_S = 240


def _skip_reason() -> str | None:
    for tool in ("node", "npm"):
        if shutil.which(tool) is None:
            return f"needs {tool} on PATH"
    if not (APP / "node_modules").is_dir():
        return "examples/ts-app/node_modules is missing: run `npm install` in examples/ts-app"
    if not (APP / "node_modules" / "coverset-vitest" / "dist" / "index.js").is_file():
        return "coverset-vitest is not built: run `npm run refresh-reporter` in examples/ts-app"
    return None


def _npm_test(
    out: Path, *, cwd: Path = APP, npm_args: tuple[str, ...] = (), **extra: str
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "COVERSET_ROOT": str(ROOT),
        "COVERSET_OUT": str(out),
        "CI": "1",
        "NO_COLOR": "1",
        **extra,
    }
    env.pop("COVERSET_SHARD", None)
    proc = subprocess.run(
        ["npm", "test", *(("--", *npm_args) if npm_args else ())],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=NPM_TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    assert (out / "manifest.json").is_file(), proc.stdout[-2000:]
    return proc


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict:  # type: ignore[type-arg]
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(reason)
    tmp = tmp_path_factory.mktemp("vitest")
    out = tmp / "neutral"
    proc = _npm_test(out)
    return {"tmp": tmp, "out": out, "stdout": proc.stdout}


@pytest.fixture(scope="module")
def ingested(run: dict) -> dict:  # type: ignore[type-arg]
    tmp, out = run["tmp"], run["out"]
    tarball = tmp / "neutral.tar.gz"
    pack(out, tarball)
    bundle = read_any_bundle(tarball)
    head = frozenset({*bundle.static, *(t.file for t in bundle.collected)})
    ctx = RunContext(
        REPO, RUN, "a" * 40, "b" * 40, "feature/ts", 3, datetime(2026, 10, 11, 12, tzinfo=UTC), None
    )
    job = IngestJob(
        facts_dir=str(tmp / "facts"),
        blob_url=local_blob_url(tmp / "facts"),
        ctx=ctx,
        shard_id=0,
        tarball=str(tarball),
        limits=BundleLimits(),
        source_root=str(ROOT),
        head_paths=head,
        functions=(),
        language="typescript",
    )
    outcome = ingest_one(job)
    assert outcome.ok, outcome.error
    store = local_facts_store(tmp / "facts")
    finalized = finalize_run(
        store, ctx, expected_shards=1, catalogue=NeutralAdapter.from_bundle(bundle).shape_catalogue()
    )
    return {"store": store, "bundle": bundle, "outcome": outcome, "finalized": finalized}


def _region(bundle, qualname: str):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN202
    return next(r for r in bundle.functions if r.qualname == qualname)


def test_the_bundle_validates_with_no_problem(run: dict) -> None:  # type: ignore[type-arg]
    assert validate(run["out"]) == []
    manifest = json.loads((run["out"] / "manifest.json").read_text())
    assert manifest["language"] == "typescript"
    assert manifest["producer"]["name"] == "coverset-vitest"
    assert manifest["meta"]["vitest"] and manifest["meta"]["node"]
    assert manifest["meta"]["recorder_errors"] == 0 and manifest["meta"]["dropped"] == {}


def _tests_without_timings(path: Path) -> list[dict]:  # type: ignore[type-arg]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [{k: v for k, v in row.items() if k not in ("duration_s", "start", "stop")} for row in rows]


def test_the_instrumentation_changes_no_statement_branch_or_line(run: dict) -> None:  # type: ignore[type-arg]
    plain = run["tmp"] / "neutral-no-shapes"
    _npm_test(plain, COVERSET_SHAPES="0")
    for name in ("files.jsonl", "coverage.jsonl", "arcs.jsonl", "functions.jsonl"):
        assert (plain / name).read_text() == (run["out"] / name).read_text(), name
    # Outcomes, messages and failure signatures (stack lines) too.
    assert _tests_without_timings(plain / "tests.jsonl") == _tests_without_timings(run["out"] / "tests.jsonl")
    assert (plain / "shapes.jsonl").read_text() == ""
    assert (run["out"] / "shapes.jsonl").read_text() != ""


def test_stack_traces_name_the_original_line(ingested: dict) -> None:  # type: ignore[type-arg]
    # The example's own check: a frame thrown from src/email.ts names the `throw` line, with
    # Istanbul and the plugin both instrumenting the file (it failed when Istanbul kept the code
    # of one pass and the source map of the other).
    outcomes = {t.nodeid: t.outcome for t in ingested["bundle"].tests}
    assert outcomes[f"{EMAIL_TESTS} > validateEmail > reports the line it throws on"] == "passed"


def test_tests_and_outcomes(ingested: dict) -> None:  # type: ignore[type-arg]
    bundle = ingested["bundle"]
    outcomes = {t.nodeid: t.outcome for t in bundle.tests}
    assert outcomes[f"{PRICING_TESTS} > applyDiscount > takes 10% off"] == "passed"
    assert outcomes[f"{PRICING_TESTS} > formatPrice > formats currencies without minor units"] == "skipped"
    assert outcomes[f"{PREFIX}/tests/shipping.test.ts > rateFor > ships worldwide"] == "skipped"
    summary = queries.run_summary(ingested["store"], REPO, RUN)
    assert summary is not None and summary.outcomes == {"passed": 19, "skipped": 2}
    collected = {c.nodeid: c for c in bundle.collected}
    assert collected[f"{PRICING_TESTS} > formatPrice > always shows two decimals"].hypothesis  # fast-check
    assert not collected[f"{PRICING_TESTS} > formatPrice > formats dollars"].hypothesis
    invariant = collected[f"{PRICING_TESTS} > applyDiscount > takes 10% off"]
    assert invariant.invariant_ids == ("discount_never_negative",)
    todo = collected[f"{PRICING_TESTS} > formatPrice > formats currencies without minor units"]
    assert todo.markers == ("todo",)


def test_per_test_coverage_of_one_test(ingested: dict) -> None:  # type: ignore[type-arg]
    store, bundle = ingested["store"], ingested["bundle"]
    apply = _region(bundle, "applyDiscount")
    # applyDiscount: `if (percent <= 0)` on 11, `return totalCents` on 12, the discount on 14-15.
    assert queries.line_tests(store, REPO, RUN, PRICING, 12) == [
        f"{PRICING_TESTS} > applyDiscount > leaves the total alone without a discount"
    ]
    assert queries.line_tests(store, REPO, RUN, PRICING, 14) == [
        f"{PRICING_TESTS} > applyDiscount > rounds half a cent up",
        f"{PRICING_TESTS} > applyDiscount > takes 10% off",
    ]
    assert queries.tests_covering(store, REPO, RUN, PRICING, apply.body_start, apply.end_line) == [
        f"{PRICING_TESTS} > applyDiscount > leaves the total alone without a discount",
        f"{PRICING_TESTS} > applyDiscount > rounds half a cent up",
        f"{PRICING_TESTS} > applyDiscount > takes 10% off",
    ]
    # The enum and the lookup table run at import, under <import> only.
    assert queries.line_tests(store, REPO, RUN, PRICING, 7) == ["<import>"]
    assert ingested["outcome"].union_lines is not None
    assert ingested["finalized"].coverage_complete


def test_regions_and_declared_types(ingested: dict) -> None:  # type: ignore[type-arg]
    bundle, store = ingested["bundle"], ingested["store"]
    assert {r.qualname for r in bundle.functions if r.path == PRICING} >= {
        "<module>",
        "applyDiscount",
        "formatPrice",
        "Cart",
        "Cart.addItem",
        "Cart.count",
    }
    apply = _region(bundle, "applyDiscount")
    # The JSDoc on line 9 is neither the declaration nor the body.
    assert (apply.def_line, apply.body_start, apply.end_line) == (10, 11, 16)
    functions = store.read_table(REPO, RUN, "functions").to_pylist()
    rate = next(f for f in functions if f["qualname"] == "rateFor")
    assert rate["language"] == "typescript"
    types = json.loads(rate["types_json"])
    assert types["zone"] == {"kind": "literal", "values": ["domestic", "eu", "world"]}
    assert types["weightsGrams"] == {"kind": "list", "elem": {"kind": "float"}}
    fmt = next(f for f in functions if f["qualname"] == "formatPrice")
    assert json.loads(fmt["types_json"])["currency"]["members"] == ["USD", "EUR", "GBP"]


def test_never_exercised_input_classes(ingested: dict) -> None:  # type: ignore[type-arg]
    store, bundle = ingested["store"], ingested["bundle"]
    apply = _region(bundle, "applyDiscount")
    stored = queries.function_input_cov(store, REPO, RUN, apply.func_key)
    assert stored is not None and stored.tests_truncated == 0
    assert "totalCents:negative" in stored.missing["totalCents"]
    cov = function_catalogue_from_types(apply, bundle.types[apply.triple]).input_cov(
        apply, [o for o in bundle.shapes if o.func == apply.triple], None
    )
    assert cov.missing_reason == NEVER_EXERCISED

    email = _region(bundle, "validateEmail")
    stored = queries.function_input_cov(store, REPO, RUN, email.func_key)
    assert stored is not None
    assert "email:multi-label-domain" in stored.missing["email"]
    assert "email:plain" not in stored.missing["email"]

    merge = _region(bundle, "mergeRanges")
    stored = queries.function_input_cov(store, REPO, RUN, merge.func_key)
    assert stored is not None and "ranges:unsorted" in stored.missing["ranges"]

    rate = _region(bundle, "rateFor")
    stored = queries.function_input_cov(store, REPO, RUN, rate.func_key)
    assert stored is not None and "weightsGrams:empty" in stored.missing["weightsGrams"]


def test_literal_and_enum_classes_come_from_the_descriptors(ingested: dict) -> None:  # type: ignore[type-arg]
    bundle = ingested["bundle"]
    rate = _region(bundle, "rateFor")
    cov = function_catalogue_from_types(rate, bundle.types[rate.triple]).input_cov(
        rate, [o for o in bundle.shapes if o.func == rate.triple], None
    )
    expected = {c.name for c in cov.expected}
    assert {"zone:'domestic'", "zone:'eu'", "zone:'world'"} <= expected
    assert {"zone:'domestic'", "zone:'eu'"} <= set(cov.observed)
    assert "zone:'world'" in {c.name for c in cov.missing}

    fmt = _region(bundle, "formatPrice")
    sampling = next(s for s in bundle.sampling if s.func == fmt.triple)
    assert sampling.truncated and sampling.cap == "sample"  # the fast-check property hit the adaptive cap
    enum_cov = function_catalogue_from_types(fmt, bundle.types[fmt.triple]).input_cov(
        fmt, [o for o in bundle.shapes if o.func == fmt.triple], sampling
    )
    assert {"currency:Currency.USD", "currency:Currency.EUR"} <= set(enum_cov.observed)
    assert "currency:Currency.GBP" in {c.name for c in enum_cov.missing}


def test_shapes_name_callers_and_receivers(ingested: dict) -> None:  # type: ignore[type-arg]
    bundle = ingested["bundle"]
    rate = _region(bundle, "rateFor")
    inner = _region(bundle, "rateFor.<anonymous>")
    callers = {o.caller for o in bundle.shapes if o.func == inner.triple}
    assert callers == {rate.triple}  # the reduce callback is called from rateFor, not from a test
    add = _region(bundle, "Cart.addItem")
    receivers = {o.shape_key for o in bundle.shapes if o.func == add.triple and o.param == "<receiver>"}
    assert receivers == {"obj:examples.ts-app.src.pricing.Cart"}
    examples = {o.example for o in bundle.shapes if o.func == _region(bundle, "validateEmail").triple}
    assert '"b***@acme.com"' in examples  # the local part is redacted at the source


def test_the_report_shows_sources_and_declared_types(run: dict, tmp_path: Path) -> None:  # type: ignore[type-arg]
    # A copy of the sources outside any git work tree: the report reads every file under it.
    sources = tmp_path / "repo"
    shutil.copytree(APP / "src", sources / PREFIX / "src")
    shutil.copytree(APP / "tests", sources / PREFIX / "tests")
    report = tmp_path / "report"
    coverset = shutil.which("coverset") or str(Path(sys.executable).with_name("coverset"))
    proc = subprocess.run(
        [coverset, "report", str(run["out"]), "--repo-root", str(sources), "--out", str(report)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    pages = {p.name: p.read_text() for p in (report / "functions").glob("*.html")}
    rate = next(text for text in pages.values() if "<title>rateFor" in text)
    assert "weightsGrams: list[float]" in rate
    assert "never exercised" in rate
    apply = next(text for text in pages.values() if "<title>applyDiscount" in text)
    assert "applyDiscount(totalCents: float, percent: float) -&gt; float" in apply
    assert "Math.round" in re.sub(r"<[^>]+>", "", apply)  # the source was read (and tokenised)


# -- parallelism ------------------------------------------------------------------------------------


def _sorted_lines(path: Path) -> list[str]:
    return sorted(path.read_text().splitlines())


@pytest.mark.parametrize(
    "args", [("--pool=threads",), ("--no-file-parallelism",)], ids=["threads", "one-file-at-a-time"]
)
def test_pools_and_file_parallelism_give_the_same_bundle(run: dict, args: tuple[str, ...]) -> None:  # type: ignore[type-arg]
    # Each worker spools to its own file, so how test files are spread over workers (forks by
    # default, worker threads, or one file at a time) changes nothing in the bundle.
    out = run["tmp"] / f"neutral-{args[0].strip('-')}"
    _npm_test(out, npm_args=args)
    for name in (
        "files.jsonl",
        "coverage.jsonl",
        "arcs.jsonl",
        "functions.jsonl",
        "shapes.jsonl",
        "links.jsonl",
    ):
        assert _sorted_lines(out / name) == _sorted_lines(run["out"] / name), name
    assert sorted(map(json.dumps, _tests_without_timings(out / "tests.jsonl"))) == sorted(
        map(json.dumps, _tests_without_timings(run["out"] / "tests.jsonl"))
    )


OVERLAP_TESTS = """
import { describe, expect, it } from "vitest";
import { rateFor } from "../src/shipping";
import { mergeRanges } from "../src/ranges";

const tick = () => new Promise((resolve) => setTimeout(resolve, 5));

describe%s("overlap", () => {
  it("ships", async () => {
    await tick();
    expect(rateFor("domestic", [100])).toBe(300);
    await tick();
  });

  it("merges", async () => {
    await tick();
    expect(mergeRanges([[1, 3], [2, 5]])).toEqual([[1, 5]]);
    await tick();
  });
});
"""


def _overlap_project(root: Path, concurrent: bool) -> Path:
    """The example's sources and config with one test file of two tests that await (``describe.concurrent``
    or plain ``describe``); ``node_modules`` is the example's, linked."""
    root.mkdir(parents=True)
    shutil.copytree(APP / "src", root / "src")
    for name in ("package.json", "tsconfig.json", "vitest.config.ts"):
        shutil.copy2(APP / name, root / name)
    (root / "tests").mkdir()
    (root / "tests" / "overlap.test.ts").write_text(OVERLAP_TESTS % (".concurrent" if concurrent else ""))
    (root / "node_modules").symlink_to(APP / "node_modules", target_is_directory=True)
    return root


def _coverage_by_test(out: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for line in (out / "coverage.jsonl").read_text().splitlines():
        row = json.loads(line)
        found.setdefault(row["test"].rsplit(" > ", 1)[-1], set()).add(row["file"])
    return found


def _shape_tests(out: Path, qualname: str) -> set[str]:
    rows = [json.loads(line) for line in (out / "shapes.jsonl").read_text().splitlines()]
    return {
        t.rsplit(" > ", 1)[-1]
        for r in rows
        if r["kind"] == "obs" and r["func"][1] == qualname
        for t in r["tests"]
    }


def test_concurrent_tests_keep_every_line_they_may_have_run_and_are_flagged(tmp_path: Path) -> None:
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(reason)
    runs = {
        "sequential": (False, ()),
        "concurrent": (True, ()),
        "one-at-a-time": (True, ("--maxConcurrency=1",)),
    }
    results = {}
    for name, (concurrent, args) in runs.items():
        project = _overlap_project(tmp_path / name, concurrent)
        out = project / "out"
        proc = _npm_test(out, cwd=project, npm_args=args, COVERSET_ROOT=str(project))
        results[name] = (out, proc.stdout + proc.stderr)

    exact = {"<import>": {"src/shipping.ts"}, "ships": {"src/shipping.ts"}, "merges": {"src/ranges.ts"}}
    for name in ("sequential", "one-at-a-time"):
        out, _ = results[name]
        assert _coverage_by_test(out) == exact, name
        assert json.loads((out / "manifest.json").read_text())["meta"]["warnings"] == [], name

    concurrent, output = results["concurrent"]
    # Istanbul's counters are shared: each test gets everything that ran while it was active (it
    # used to be: "ships" got both tests' lines and "merges" none at all).
    assert _coverage_by_test(concurrent) == {
        "<import>": {"src/shipping.ts"},
        "ships": {"src/shipping.ts", "src/ranges.ts"},
        "merges": {"src/shipping.ts", "src/ranges.ts"},
    }
    # Shapes follow each test's own async continuation: exact (they used to go to "merges").
    assert _shape_tests(concurrent, "rateFor") == {"ships"}
    assert _shape_tests(concurrent, "mergeRanges") == {"merges"}
    meta = json.loads((concurrent / "manifest.json").read_text())["meta"]
    assert (meta["concurrent_tests"], meta["overlapping_tests"]) == (2, 2)
    assert len(meta["warnings"]) == 1 and "--maxConcurrency=1" in meta["warnings"][0]
    assert "coverset-vitest: 2 test(s) ran while another test of the same worker was running" in output
    rows = [json.loads(line) for line in (concurrent / "tests.jsonl").read_text().splitlines()]
    assert {tuple(row["markers"]) for row in rows} == {("concurrent",)}
    assert validate(concurrent) == []
