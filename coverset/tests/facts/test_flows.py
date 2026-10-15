"""Production flows in the facts store: validation, import (idempotent, atomic), function resolution, reads."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from facts_helpers import MODULE_PATH, T_NEG, T_POS, int_catalogue, make_ctx

from coverset.facts import flows
from coverset.facts.convert import read_python_bundle
from coverset.facts.flows import (
    PROD_FLOWS_SCHEMA,
    caller_hash,
    caller_index,
    import_payloads,
    list_windows,
    prune_windows,
    read_payload_file,
    read_prod,
    resolve_functions,
    run_shapes_by_function,
    validate_payload,
)
from coverset.facts.ingest import finalize_run, ingest_shard
from coverset.facts.store import FactsStore
from coverset.lang.base import FunctionRegion

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO = 1


def payload(
    start: str = "2026-10-13T10:00:00Z", end: str = "2026-10-13T10:01:00Z", **changes: Any
) -> dict[str, Any]:
    """A valid window: classify() called with positive ints (tested) and NaN floats (not)."""
    base: dict[str, Any] = {
        "version": 1,
        "kind": "coverset.prod-flows",
        "service": "calc-api",
        "env": "production",
        "release": "abc123",
        "window": {"start": start, "end": end},
        "probe": {"k": 5, "sample_rate": 0.01},
        "funcs": [
            {
                "module": "factspkg.calc",
                "qualname": "classify",
                "line": 9,
                "path": None,
                "calls": 900,
                "sampled": 9,
            },
            {
                "module": "factspkg.calc",
                "qualname": "add",
                "line": 5,
                "path": "src/factspkg/calc.py",
                "calls": 100,
            },
            {"module": "factspkg.gone", "qualname": "old", "line": 1, "path": None, "calls": 3},
        ],
        "rows": [
            {
                "f": 0,
                "p": "n",
                "s": "int|pos",
                "calls": 700.0,
                "traces": 40,
                "callers": {"<external>": 700.0},
            },
            {"f": 0, "p": "n", "s": "float|nan", "calls": 200.0, "traces": 12, "callers": {}},
            {"f": 1, "p": "a", "s": "int|pos", "calls": 100.0, "traces": 30},
        ],
        "calls": [{"f": 0, "v": [["n", "int|pos"]], "calls": 700.0, "traces": 40}],
        "flows": [{"fs": [0, 1], "traces": 9}],
    }
    base.update(changes)
    return base


def lines(*payloads: dict[str, Any]) -> list[tuple[int, dict[str, Any] | None, str | None]]:
    return [(n, copy.deepcopy(p), None) for n, p in enumerate(payloads, start=1)]


@pytest.fixture
def run_store(store: FactsStore, minimal_bundle: Path) -> FactsStore:
    """The minimal bundle ingested as run-1 (classify saw -3, 5, 1 and 0 from its tests)."""
    from facts_helpers import calc_functions

    ctx = make_ctx("run-1", repo_id=REPO, day=1)
    ingest_shard(store, ctx, read_python_bundle(minimal_bundle), 0, calc_functions())
    finalize_run(store, ctx, expected_shards=1, catalogue=int_catalogue)
    return store


def test_the_documented_schema_is_the_code_schema() -> None:
    documented = json.loads((PROJECT_ROOT / "docs" / "schema" / "prod-flows.v1.json").read_text())
    assert documented == PROD_FLOWS_SCHEMA, (
        "regenerate docs/schema/prod-flows.v1.json (see PROD_FLOWS_SCHEMA)"
    )


def test_the_fixture_windows_are_valid() -> None:
    for number, data, problem in read_payload_file(PROJECT_ROOT / "fixtures" / "flows" / "ledger-prod.jsonl"):
        assert problem is None and data is not None
        assert validate_payload(data) == [], number


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"version": 2}, "version"),
        ({"service": "a/b"}, "service"),
        ({"extra": 1}, "Additional properties"),
        ({"window": {"start": "2026-10-13T10:01:00Z", "end": "2026-10-13T10:00:00Z"}}, "end is before start"),
        ({"window": {"start": "yesterday", "end": "2026-10-13T10:00:00Z"}}, "window/start"),
        ({"rows": [{"f": 7, "p": "n", "s": "int|pos", "calls": 1.0}]}, "rows/0/f: no function 7"),
        ({"rows": [{"f": 0, "p": "n", "s": "alice@example.com", "calls": 1.0}]}, "unknown type token"),
        ({"rows": [{"f": 0, "p": "n", "s": "int|pos", "calls": -1}]}, "rows/0/calls"),
        ({"rows": [{"f": 0, "p": "n", "s": "int|pos", "calls": 1, "callers": {"bob": 1}}]}, "callers"),
        ({"calls": [{"f": 0, "v": [["n", "free text"]], "calls": 1.0}]}, "shape key 'free text'"),
        ({"flows": [{"fs": [0, 9]}]}, "flows/0/fs"),
    ],
)
def test_validation_refuses_bad_windows(change: dict[str, Any], expected: str) -> None:
    problems = validate_payload(payload(**change))
    assert any(expected in p for p in problems), problems


def test_read_payload_file_handles_gzip_and_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "flows.jsonl.gz"
    with gzip.open(path, "wt") as out:
        out.write(json.dumps(payload()) + "\n\nnot json\n[1, 2]\n")
    read = list(read_payload_file(path))
    assert [(n, p is not None, problem) for n, p, problem in read][1:] == [
        (3, False, read[1][2]),
        (4, False, "not a JSON object"),
    ]
    assert read[0][1] == payload()
    assert read[1][2].startswith("not JSON")


def test_oversized_lines_and_files_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "flows.jsonl"
    path.write_text(json.dumps(payload()) + "\n" + json.dumps(payload(release="b")) + "\n")
    monkeypatch.setattr(flows, "MAX_LINE_BYTES", 100)
    assert [problem for _, _, problem in read_payload_file(path)] == ["line is longer than 100 bytes"] * 2
    monkeypatch.setattr(flows, "MAX_FILE_BYTES", 50)
    with pytest.raises(flows.FlowsError, match="expands beyond"):
        list(read_payload_file(path))
    with pytest.raises(flows.FlowsError):
        list(read_payload_file(tmp_path / "missing.jsonl"))


def test_import_stores_a_batch_and_is_idempotent(run_store: FactsStore) -> None:
    regions = _regions(run_store)
    first = import_payloads(run_store, REPO, lines(payload(), payload()), regions=regions)
    assert (first.windows, first.duplicates, first.rows, first.funcs, first.unresolved) == (1, 1, 3, 3, 1)
    assert first.services == {"calc-api"}
    [batch] = first.batches
    assert batch.parent.name == "service=calc-api" and batch.parent.parent.name == "prod"
    assert sorted(p.name for p in batch.iterdir()) == [f"{t}.parquet" for t in sorted(flows.PROD_TABLES)]
    again = import_payloads(run_store, REPO, lines(payload()), regions=regions)
    assert (again.windows, again.duplicates, again.batches) == (0, 1, [])
    assert not any(p.name.startswith(".staging") for p in batch.parent.iterdir())
    # Production data never shows up as a run, so run views, gc and inventories ignore it.
    assert run_store.list_runs(REPO) == ["run-1"]


def test_import_rejects_bad_windows_and_keeps_the_good_ones(run_store: FactsStore) -> None:
    bad = payload(version=3)
    result = import_payloads(
        run_store, REPO, [(1, payload(), None), (2, bad, None), (3, None, "not JSON: x")], regions=[]
    )
    assert result.windows == 1
    assert [line for line, _ in result.rejected] == [2, 3]
    assert result.unresolved == 3  # no reference run: resolved again at read time


def test_service_override_and_validation(run_store: FactsStore) -> None:
    result = import_payloads(run_store, REPO, lines(payload()), service="calc-canary")
    assert result.services == {"calc-canary"}
    with pytest.raises(ValueError, match="invalid service"):
        import_payloads(run_store, REPO, lines(payload()), service="../etc")


def test_read_prod_picks_the_newest_windows_per_service(run_store: FactsStore) -> None:
    regions = _regions(run_store)
    windows = [
        payload("2026-10-13T10:00:00Z", "2026-10-13T10:01:00Z"),
        payload("2026-10-13T10:01:00Z", "2026-10-13T10:02:00Z"),
        payload("2026-10-13T10:02:00Z", "2026-10-13T10:03:00Z", service="other-api"),
    ]
    import_payloads(run_store, REPO, lines(*windows), regions=regions)
    assert [(w.service, w.start.minute) for w in list_windows(run_store, REPO)] == [
        ("calc-api", 0),
        ("calc-api", 1),
        ("other-api", 2),
    ]
    newest = read_prod(run_store, REPO, last_windows=1)
    assert [(w.service, w.start.minute) for w in newest.windows] == [("calc-api", 1), ("other-api", 2)]
    assert len(newest.shapes) == 6 and len(newest.funcs) == 6
    nan = next(r for r in newest.shapes if r["shape_key"] == "float|nan")
    assert nan["func_key"] == next(r.func_key for r in regions if r.qualname == "classify")
    assert (nan["calls"], nan["traces"], nan["k_ok"]) == (200.0, 12, True)
    assert json.loads(
        next(r for r in newest.shapes if r["param"] == "n" and r["shape_key"] == "int|pos")["callers_json"]
    )
    assert newest.calls[0]["vector"] == [("n", "int|pos")]
    assert len(newest.flows[0]["funcs"]) == 2
    only = read_prod(run_store, REPO, service="calc-api", last_windows=None)
    assert len(only.windows) == 2
    assert read_prod(run_store, REPO, service="nothing").windows == []


def test_rows_of_a_non_gating_producer_need_k_traces_in_their_window(run_store: FactsStore) -> None:
    window = payload(probe={})  # no "k": the producer did not gate
    window["rows"][1]["traces"] = 2
    window["rows"].append({"f": 1, "p": "b", "s": "int|neg", "calls": 3.0, "traces": 9, "k_ok": False})
    import_payloads(run_store, REPO, lines(window), regions=_regions(run_store))
    ok = {(r["param"], r["shape_key"]): r["k_ok"] for r in read_prod(run_store, REPO).shapes}
    assert ok == {
        ("n", "int|pos"): True,
        ("n", "float|nan"): False,
        ("a", "int|pos"): True,
        ("b", "int|neg"): False,
    }


def test_prune_deletes_batches_that_ended_before_the_cutoff(run_store: FactsStore) -> None:
    import_payloads(run_store, REPO, lines(payload()))
    cutoff = datetime(2026, 10, 14, tzinfo=UTC)
    assert len(prune_windows(run_store, REPO, older_than=cutoff, dry_run=True)) == 1
    assert list_windows(run_store, REPO)
    assert len(prune_windows(run_store, REPO, older_than=datetime(2026, 10, 1, tzinfo=UTC))) == 0
    assert len(prune_windows(run_store, REPO, older_than=cutoff)) == 1
    assert list_windows(run_store, REPO) == []


def _regions(store: FactsStore) -> list[FunctionRegion]:
    from coverset.facts.queries import ParquetPriorRun

    return ParquetPriorRun(store, REPO, "run-1").functions()


def _region(path: str, qualname: str, line: int, ordinal: int = 0) -> FunctionRegion:
    return FunctionRegion(
        f"{path}:{qualname}:{ordinal}",
        path,
        qualname,
        ordinal,
        line,
        line + 1,
        line + 5,
        "b",
        "s",
        (),
        "function",
    )


def test_resolution_by_path_module_and_closest_line() -> None:
    regions = [
        _region("src/app/mail.py", "check", 3),
        _region("src/app/mail.py", "dup", 10, 0),
        _region("src/app/mail.py", "dup", 40, 1),
        _region("lib/a/util.py", "helper", 1),
        _region("lib/b/util.py", "helper", 1),
    ]
    funcs = [
        {"module": "app.mail", "qualname": "check", "line": 3},
        {"module": "whatever", "qualname": "check", "line": 3, "path": "src/app/mail.py"},
        {"module": "app.mail", "qualname": "dup", "line": 38},
        {"module": "util", "qualname": "helper", "line": 1},  # two files fit: ambiguous
        {"module": "a.util", "qualname": "helper", "line": 1},
        {"module": "app.mail", "qualname": "missing", "line": 1},
    ]
    resolved = [r.func_key if r else None for r in resolve_functions(regions, funcs)]
    assert resolved == [
        "src/app/mail.py:check:0",
        "src/app/mail.py:check:0",
        "src/app/mail.py:dup:1",
        None,
        "lib/a/util.py:helper:0",
        None,
    ]


def test_caller_hashes_resolve_to_regions() -> None:
    region = _region("src/ledger/accounts/emailcheck.py", "validate_email", 8)
    index = caller_index([region])
    assert index[caller_hash("ledger.accounts.emailcheck", "validate_email")] is region
    assert index[caller_hash("src.ledger.accounts.emailcheck", "validate_email")] is region
    # The probe's definition (coverset_probe.tracer.caller_hash), restated: the two must never drift.
    assert caller_hash("x", "y") == hashlib.blake2b(b"x\0y", digest_size=8).hexdigest()


def test_run_shapes_by_function_pools_every_caller(run_store: FactsStore) -> None:
    classify = next(r for r in _regions(run_store) if r.qualname == "classify")
    shapes = run_shapes_by_function(run_store, REPO, "run-1", [classify.func_key, "nope"])
    assert set(shapes) == {classify.func_key}
    by_key = {(param, key): nodeids for param, key, nodeids in shapes[classify.func_key]}
    assert by_key[("n", "int|neg")] == (T_NEG,)
    assert T_POS in by_key[("n", "int|pos")]
    assert run_shapes_by_function(run_store, REPO, "run-1", []) == {}
    assert classify.path == MODULE_PATH


ROW_MUTATIONS: list[tuple[str, int, dict[str, Any]]] = [
    ("rows", 0, {"p": ""}),
    ("rows", 0, {"s": "x" * 513}),
    ("rows", 0, {"calls": True}),
    ("rows", 0, {"calls": "1"}),
    ("rows", 0, {"traces": -1}),
    ("rows", 0, {"k_ok": 1}),
    ("rows", 0, {"callers": {f"{n:016x}": 1.0 for n in range(17)}}),
    ("rows", 0, {"callers": {"<external>": -1}}),
    ("rows", 0, {"extra": 1}),
    ("rows", 0, {"f": -1}),
    ("calls", 0, {"v": [["n"]]}),
    ("calls", 0, {"v": [["n", "int|pos", "x"]]}),
    ("calls", 0, {"v": "n"}),
    ("flows", 0, {"fs": []}),
    ("flows", 0, {"fs": [0, 0]}),
    ("flows", 0, {"fs": ["0"]}),
    ("rows", 0, {}),  # unchanged: valid
]


@pytest.mark.parametrize(("kind", "index", "change"), ROW_MUTATIONS)
def test_the_fast_row_check_agrees_with_the_json_schema(
    kind: str, index: int, change: dict[str, Any]
) -> None:
    window = payload()
    window[kind][index] = {**window[kind][index], **change}
    fast = validate_payload(copy.deepcopy(window))
    full = validate_payload(copy.deepcopy(window), full_schema=True)
    assert bool(fast) == bool(full), (fast, full)


def test_a_row_that_is_not_an_object_is_refused() -> None:
    window = payload()
    window["rows"].append([1, 2])
    window["calls"] = "nope"
    problems = validate_payload(window)
    assert problems


# -- review fixes ------------------------------------------------------------------------------------


def test_an_impossible_date_rejects_only_its_window(run_store: FactsStore) -> None:
    # "2026-13-45" passes the schema's pattern; the calendar check must reject the window, not the file.
    bad = payload("2026-13-45T10:00:00Z", "2026-13-45T10:01:00Z")
    assert validate_payload(bad) == ["window: start/end is not a valid UTC time"]
    result = import_payloads(run_store, REPO, [(1, payload(), None), (2, bad, None)], regions=[])
    assert result.windows == 1
    assert result.rejected == [(2, ["window: start/end is not a valid UTC time"])]


def test_one_resolver_index_serves_every_function(monkeypatch: pytest.MonkeyPatch) -> None:
    regions = [_region(f"src/pkg/mod{i}.py", f"f{j}", 1 + 10 * j) for i in range(100) for j in range(30)]
    funcs = [{"module": f"pkg.mod{i}", "qualname": "f3", "line": 31} for i in range(500)]
    built: list[int] = []
    original = flows.FunctionResolver.__init__

    def counting(self: flows.FunctionResolver, regions: Any) -> None:
        built.append(1)
        original(self, regions)

    monkeypatch.setattr(flows.FunctionResolver, "__init__", counting)
    resolved = resolve_functions(regions, funcs)
    assert len(built) == 1
    assert [r.func_key for r in resolved if r is not None][:2] == [
        "src/pkg/mod0.py:f3:0",
        "src/pkg/mod1.py:f3:0",
    ]
    assert resolved[100:] == [None] * 400


def _window_info(service: str, start_minute: float, length_s: float = 60.0) -> flows.WindowInfo:
    from datetime import timedelta

    start = datetime(2026, 10, 13, 10, tzinfo=UTC) + timedelta(minutes=start_minute)
    return flows.WindowInfo(
        service, "production", None, start, start + timedelta(seconds=length_s), 5, 0.01, Path("b")
    )


def test_windows_are_pooled_by_time_across_processes() -> None:
    # Four workers, each flushing every 60 s but started 15 s apart, for five minutes.
    windows = [_window_info("api", m + w * 0.25) for m in range(5) for w in range(4)]
    newest = flows.select_windows(windows, last_windows=1)
    assert len(newest) == 4  # one window of each worker, not whichever flushed last
    assert len({w.start for w in newest}) == 4
    assert len(flows.select_windows(windows, last_windows=3)) == 12
    assert flows.select_windows(windows, last_windows=None) == windows
    since = datetime(2026, 10, 13, 10, 4, 30, tzinfo=UTC)
    assert all(w.end > since for w in flows.select_windows(windows, since=since))
    assert len(flows.select_windows(windows, since=since)) == 5  # ends 10:04:45 to 10:05:45
    # One process with full-length windows: exactly its newest N windows.
    single = [_window_info("api", m) for m in range(5)]
    assert [w.start.minute for w in flows.select_windows(single, last_windows=2)] == [3, 4]
    # A short last window (cut at exit) does not shrink the span: it is a time span, not a count.
    single.append(_window_info("api", 5, length_s=7))
    assert [w.start.minute for w in flows.select_windows(single, last_windows=2)] == [3, 4, 5]
    assert flows.select_windows([], last_windows=1) == []


def test_reached_comes_from_coverage_not_from_shapes(
    run_store: FactsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coverset.facts import queries

    regions = {r.qualname: r.func_key for r in _regions(run_store)}
    keys = list(regions.values())
    expected = {regions[q] for q in ("add", "classify", "Box.__init__", "Box.total")}
    assert flows.run_functions_reached(run_store, REPO, "run-1", keys) == expected  # func_body summary
    monkeypatch.setattr(queries, "summary_is_fresh", lambda *_: False)
    assert flows.run_functions_reached(run_store, REPO, "run-1", keys) == expected  # test_lines
    assert flows.run_functions_reached(run_store, REPO, "run-1", []) == set()
    assert flows.run_has_shapes(run_store, REPO, "run-1")


def test_a_run_without_shapes_is_detected(store: FactsStore, bundle_copy: Path) -> None:
    from facts_helpers import calc_functions

    (bundle_copy / "shapes.jsonl.gz").unlink()
    ctx = make_ctx("run-2", repo_id=REPO, day=2)
    ingest_shard(store, ctx, read_python_bundle(bundle_copy), 0, calc_functions())
    finalize_run(store, ctx, expected_shards=1, catalogue=int_catalogue)
    assert not flows.run_has_shapes(store, REPO, "run-2")
    classify = next(r for r in ParquetRegions(store) if r.qualname == "classify")
    assert flows.run_functions_reached(store, REPO, "run-2", [classify.func_key]) == {classify.func_key}


def ParquetRegions(store: FactsStore) -> list[FunctionRegion]:  # noqa: N802 - reads like the class it wraps
    from coverset.facts.queries import ParquetPriorRun

    return ParquetPriorRun(store, REPO, "run-2").functions()
