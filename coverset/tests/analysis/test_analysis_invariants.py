"""invariants.yaml: schema, safety, evaluation, PR diff and the coverage lens."""

from __future__ import annotations

import ast
import builtins
import dataclasses
import textwrap
from pathlib import Path

import pytest
from analysis_helpers import obs

from coverset.analysis.functions import index_functions, index_functions_detailed
from coverset.analysis.invariants import (
    Invariant,
    InvariantContext,
    InvariantError,
    PreviousResult,
    ReviewEvidence,
    diff_invariants,
    evaluate,
    inv_key,
    invariant_lens,
    load_invariants,
    resolve_target,
    spec_hash,
)
from coverset.analysis.shapes import function_input_cov

DEMO_INVARIANTS = Path(__file__).parents[2] / "examples" / "demo-app" / "invariants.yaml"
YAML = textwrap.dedent(
    """\
    version: 1
    invariants:
      - id: cents_conserved
        scope: {kind: function, target: ledger.billing.allocate.allocate_cents}
        statement: Shares always sum to the total.
        severity: error
        verify:
          - kind: test
            marker: cents_conserved
          - kind: property
          - kind: runtime
            check: "sum(result) == total_cents"
      - id: signup_rejects_existing_domain
        scope: {kind: function, target: ledger.accounts.signup.check_if_new_user_signup_allowed}
        statement: Existing customer domains cannot self-sign-up.
        verify:
          - kind: test
          - kind: review
      - id: reviewed_only
        scope: {kind: module, target: ledger.accounts.signup}
        statement: Only reviewed.
        severity: info
        verify:
          - kind: review
    """
)
ALLOC_PATH = "src/ledger/billing/allocate.py"
ALLOC_SRC = "def allocate_cents(total_cents: int, weights: list[float]) -> list[int]:\n    return []\n"
ALLOC = index_functions(ALLOC_PATH, ALLOC_SRC)[1]
T_UNIT = "tests/test_allocate.py::test_sum"
T_PROP = "tests/test_properties.py::test_cents_property"
T_OTHER = "tests/test_allocate.py::test_other"


@pytest.fixture
def invariants() -> dict[str, Invariant]:
    return {inv.id: inv for inv in load_invariants(YAML)}


def _ctx(**overrides) -> InvariantContext:
    values = {
        "results": {T_UNIT: "passed", T_PROP: "passed", T_OTHER: "failed"},
        "links": {T_UNIT: ["cents_conserved"], T_PROP: ["cents_conserved"]},
        "hypothesis": {T_PROP: True},
        "target_region": ALLOC,
    }
    values.update(overrides)
    return InvariantContext(**values)


def test_demo_invariants_validate() -> None:
    loaded = load_invariants(DEMO_INVARIANTS.read_text())
    assert [inv.id for inv in loaded] == [
        "cents_conserved",
        "durations_consume_whole_input",
        "merge_idempotent",
        "signup_rejects_existing_domain",
    ]
    assert loaded[0].verify[0].marker == "cents_conserved"
    assert loaded[2].severity == "warning"


def test_defaults_keys_and_hash(invariants: dict[str, Invariant]) -> None:
    signup = invariants["signup_rejects_existing_domain"]
    assert signup.severity == "error"
    assert inv_key(signup) == "signup_rejects_existing_domain"
    assert len(spec_hash(signup)) == 64
    same = load_invariants(YAML.replace("Existing customer domains", "Existing customer domains"))
    assert spec_hash(same[1]) == spec_hash(signup)
    reworded = load_invariants(YAML.replace("cannot self-sign-up", "may not self-sign-up"))
    assert spec_hash(reworded[1]) != spec_hash(signup)


def test_bare_list_and_empty_documents() -> None:
    assert load_invariants("") == []
    listed = load_invariants(
        "- id: a\n  scope: {kind: package, target: ledger}\n  statement: s\n  verify: [{kind: test}]\n"
    )
    assert listed[0].scope.kind == "package"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("invariants:\n  - id: a\n    scope: {kind: galaxy, target: x}\n    statement: s\n    verify: [{kind: test}]\n", "schema"),
        ("invariants:\n  - id: a\n    scope: {kind: module, target: x}\n    statement: s\n    verify: []\n", "schema"),
        ("invariants:\n  - id: a\n    scope: {kind: module, target: x}\n    statement: s\n    verify: [{kind: test}]\n    extra: 1\n", "schema"),
        ("invariants:\n  - id: '1bad id'\n    scope: {kind: module, target: x}\n    statement: s\n    verify: [{kind: test}]\n", "schema"),
        ("invariants:\n  - id: a\n    scope: {kind: module, target: x}\n    statement: s\n    verify: [{kind: test, check: 'x ==='}]\n", "not a valid expression"),
        ("invariants:\n  - id: a\n    scope: {kind: module, target: x}\n    statement: s\n    verify: [{kind: test, check: 'x = 1'}]\n", "not a valid expression"),
        ("invariants:\n  - {id: a, scope: {kind: module, target: x}, statement: s, verify: [{kind: test}]}\n  - {id: a, scope: {kind: module, target: x}, statement: s, verify: [{kind: test}]}\n", "duplicate"),
        ("invariants: [unclosed", "invalid YAML"),
        ("version: 2\ninvariants: []\n", "schema"),
    ],
)  # fmt: skip
def test_invalid_documents_are_rejected(text: str, message: str) -> None:
    with pytest.raises(InvariantError, match=message):
        load_invariants(text)


def test_size_cap() -> None:
    with pytest.raises(InvariantError, match="exceeds"):
        load_invariants("#" * 2048, max_bytes=1024)


@pytest.mark.security
def test_python_object_tags_are_refused(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    payloads = [
        f"!!python/object/apply:os.system ['touch {marker}']",
        f"invariants: !!python/object/apply:subprocess.check_output [['touch', '{marker}']]",
        "!!python/name:os.system",
    ]
    for text in payloads:
        with pytest.raises(InvariantError, match="invalid YAML"):
            load_invariants(text)
    assert not marker.exists()


@pytest.mark.security
def test_alias_bombs_are_refused() -> None:
    bomb = "a: &a ['x','x']\nb: &b [*a,*a]\nc: &c [*b,*b]\ninvariants: [*c]\n"
    with pytest.raises(InvariantError, match="aliases"):
        load_invariants(bomb)


@pytest.mark.security
def test_check_expressions_are_never_evaluated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "pwned"
    check = f"__import__('os').system('touch {marker}')"
    text = (
        "invariants:\n  - id: a\n    scope: {kind: function, target: m.f}\n    statement: s\n"
        f'    verify: [{{kind: runtime, check: "{check}"}}]\n'
    )
    calls: list[str] = []

    def spy(name: str, real):
        def wrapper(source, *args, **kwargs):
            flags = args[2] if len(args) > 2 else kwargs.get("flags", 0)
            if "__import__" in str(source) and not flags & ast.PyCF_ONLY_AST:
                calls.append(name)
            return real(source, *args, **kwargs)

        return wrapper

    for name in ("eval", "exec", "compile"):
        monkeypatch.setattr(builtins, name, spy(name, getattr(builtins, name)))
    (inv,) = load_invariants(text)
    result = evaluate(inv, _ctx())
    monkeypatch.undo()
    assert inv.verify[0].check == check
    assert result.status == "untested"
    assert result.evidence["entries"][0]["reason"] == "runtime checks not implemented"
    assert calls == []
    assert not marker.exists()


def test_test_and_property_entries(invariants: dict[str, Invariant]) -> None:
    cents = invariants["cents_conserved"]
    result = evaluate(cents, _ctx())
    assert result.status == "satisfied"
    assert result.covering_test_ids == [T_UNIT, T_PROP]
    entries = {e["kind"]: e for e in result.evidence["entries"]}
    assert entries["test"]["ran"] == 2 and entries["property"]["ran"] == 1

    failing = evaluate(cents, _ctx(results={T_UNIT: "passed", T_PROP: "failed"}))
    assert failing.status == "violated"
    assert {e["kind"]: e["failed"] for e in failing.evidence["entries"] if "failed" in e} == {
        "test": [T_PROP],
        "property": [T_PROP],
    }
    none_ran = evaluate(cents, _ctx(results={T_UNIT: "skipped", T_PROP: "not_run"}))
    assert none_ran.status == "untested"
    assert none_ran.covering_test_ids == []


def test_property_requires_hypothesis_tests(invariants: dict[str, Invariant]) -> None:
    only_property = dataclasses.replace(
        invariants["cents_conserved"], verify=invariants["cents_conserved"].verify[1:2]
    )
    assert evaluate(only_property, _ctx(hypothesis={})).status == "untested"
    assert evaluate(only_property, _ctx()).covering_test_ids == [T_PROP]


def test_test_ids_select_by_nodeid_prefix() -> None:
    (inv,) = load_invariants(
        "- id: a\n  scope: {kind: function, target: m.f}\n  statement: s\n"
        "  verify: [{kind: test, test_ids: ['tests/test_allocate.py::test_sum']}]\n"
    )
    results = {
        "tests/test_allocate.py::test_sum[1]": "passed",
        "tests/test_allocate.py::test_summary": "failed",
    }
    result = evaluate(inv, _ctx(results=results, links={}))
    assert (result.status, result.covering_test_ids) == ("satisfied", ["tests/test_allocate.py::test_sum[1]"])


def test_review_entries_are_advisory(invariants: dict[str, Invariant]) -> None:
    signup = invariants["signup_rejects_existing_domain"]
    flagged = ReviewEvidence(ALLOC.body_hash, "violated", "bob@eng.acme.com is allowed", "fixture")
    # No linked test ran: the test entry decides (untested); the AI finding is attached as evidence only.
    result = evaluate(signup, _ctx(links={}, review_finding=flagged))
    assert result.status == "untested"
    assert result.evidence["entries"][1]["finding"]["status"] == "violated"
    # A passing linked test is not overridden by an AI violation.
    linked = {T_UNIT: ["signup_rejects_existing_domain"]}
    assert evaluate(signup, _ctx(links=linked, review_finding=flagged)).status == "satisfied"

    reviewed_only = invariants["reviewed_only"]
    assert evaluate(reviewed_only, _ctx()).status == "needs_review"
    assert evaluate(reviewed_only, _ctx(review_finding=flagged)).status == "needs_review"
    stale_finding = ReviewEvidence("other-body", "satisfied", "ok", "fixture")
    assert evaluate(reviewed_only, _ctx(review_finding=stale_finding)).status == "needs_review"
    ok = ReviewEvidence(ALLOC.body_hash, "satisfied", "ok", "fixture")
    assert evaluate(reviewed_only, _ctx(review_finding=ok)).status == "satisfied"


def test_stale_and_carried_forward(invariants: dict[str, Invariant]) -> None:
    cents = invariants["cents_conserved"]
    no_run = _ctx(results={})
    previous = PreviousResult("run-7", "satisfied", ALLOC.body_hash, spec_hash(cents))
    carried = evaluate(cents, dataclasses.replace(no_run, previous_result=previous))
    assert (carried.status, carried.evidence["carried_from_run"]) == ("satisfied", "run-7")
    body_changed = dataclasses.replace(previous, target_body_hash="0" * 16)
    stale = evaluate(cents, dataclasses.replace(no_run, previous_result=body_changed))
    assert (stale.status, stale.evidence["reason"]) == ("stale", "target body changed")
    spec_changed = dataclasses.replace(previous, spec_hash="1" * 64)
    assert evaluate(cents, dataclasses.replace(no_run, previous_result=spec_changed)).evidence["reason"] == (
        "spec changed"
    )
    previously_violated = dataclasses.replace(previous, status="violated")
    assert (
        evaluate(cents, dataclasses.replace(no_run, previous_result=previously_violated)).status == "untested"
    )
    # Fresh evidence beats the previous result.
    assert evaluate(cents, _ctx(previous_result=body_changed)).status == "satisfied"


def test_diff_invariants(invariants: dict[str, Invariant]) -> None:
    base = list(invariants.values())
    weakened_text = (
        YAML.replace("severity: error\n", "severity: warning\n", 1)
        .replace("      - kind: property\n", "", 1)
        .replace("Only reviewed.", "Only reviewed, reworded.")
    )
    head = [inv for inv in load_invariants(weakened_text) if inv.id != "signup_rejects_existing_domain"]
    head += load_invariants(
        "- id: new_rule\n  scope: {kind: function, target: m.f}\n  statement: s\n  verify: [{kind: test}]\n"
    )
    changes = {c.inv_id: c for c in diff_invariants(base, head)}
    assert changes["cents_conserved"].kind == "weakened"
    assert changes["cents_conserved"].reasons == (
        "severity lowered from error to warning",
        "verify entry removed or narrowed: property",
    )
    assert changes["signup_rejects_existing_domain"].kind == "removed"
    assert changes["reviewed_only"].kind == "changed"
    assert changes["reviewed_only"].reasons == ("statement changed",)
    assert changes["new_rule"].kind == "added"
    assert [c.kind for c in diff_invariants(base, base)] == ["unchanged"] * 3


def test_resolve_target(invariants: dict[str, Invariant]) -> None:
    regions = index_functions(ALLOC_PATH, ALLOC_SRC)
    assert resolve_target(invariants["cents_conserved"], regions, "src") == ALLOC
    assert resolve_target(invariants["cents_conserved"], regions, ".") is None
    signup_regions = index_functions("src/ledger/accounts/signup.py", "def f():\n    pass\n")
    module = resolve_target(invariants["reviewed_only"], signup_regions, "src")
    assert module is not None and module.kind == "module"


def test_invariant_lens(invariants: dict[str, Invariant]) -> None:
    detailed = index_functions_detailed(ALLOC_PATH, ALLOC_SRC)[1]
    observations = [
        obs("total_cents", "int|pos", tests=(T_UNIT,)),
        obs("total_cents", "int|neg", tests=(T_OTHER,)),
        obs("weights", "list|elem:int|len:2-10|order:sorted|any_zero", tests=(T_OTHER,)),
    ]
    cov = function_input_cov(
        detailed.region,
        observations,
        None,
        annotations=dict(zip(detailed.region.params, detailed.annotations, strict=True)),
    )
    lens = invariant_lens(invariants["cents_conserved"], cov, [T_UNIT, T_PROP], covers_changed_lines=True)
    assert lens.tests == [T_UNIT, T_PROP]
    assert lens.reached == ["total_cents:positive"]
    assert "weights:zero-element" in {c.name for c in lens.unreached}
    assert lens.summary.startswith("cents_conserved: 2 tests, never reached total_cents:negative")
    empty = invariant_lens(invariants["reviewed_only"], None, [], covers_changed_lines=False)
    assert empty.summary == "reviewed_only: no linked tests; changed lines not covered by linked tests"


def test_xfailed_linked_test_is_a_known_violation(invariants: dict[str, Invariant]) -> None:
    only_test = dataclasses.replace(
        invariants["cents_conserved"], verify=invariants["cents_conserved"].verify[:1]
    )
    result = evaluate(only_test, _ctx(results={T_UNIT: "xfailed", T_PROP: "passed"}))
    assert result.status == "violated"
    (entry,) = result.evidence["entries"]
    assert (entry["xfailed"], entry["failed"]) == ([T_UNIT], [])
    assert "xfail" in entry["reason"]
    assert evaluate(only_test, _ctx(results={T_UNIT: "xpassed", T_PROP: "passed"})).status == "satisfied"
