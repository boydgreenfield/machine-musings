"""Invariants: user-declared behavioural rules, their schema, evaluation and PR diff.

``invariants.yaml`` is untrusted input. It is parsed with a YAML safe loader that also refuses
aliases (so alias bombs cannot blow up validation), capped at 1 MiB, and validated against
:data:`INVARIANTS_SCHEMA`. ``check`` expressions are only syntax-checked with
``ast.parse(mode="eval")``; nothing here ever evaluates, executes or compiles them.

Evaluation per ``verify`` entry:

``test``
    satisfied if at least one linked test ran and all of them passed (``xpassed`` counts: the
    assertion held), violated if any failed, errored or *xfailed* (an ``xfail`` mark on a linked
    test documents a known violation of the rule), untested if none ran. Tests are linked by ``test_ids`` (exact nodeid, parametrisations of it,
    or a file/class prefix), else by ``@pytest.mark.invariant(<marker or id>)``.
``property``
    the same over linked tests that are Hypothesis tests.
``runtime``
    accepted but not executed in v1: untested, "runtime checks not implemented".
``review``
    needs_review until a review finding exists for the target's current body hash. Review
    findings are advisory: they never override a test or property outcome and never yield
    ``violated``; on their own they can mark an invariant satisfied or keep it needs_review.

Test and property outcomes win (violated over satisfied over untested). When a run brings no
fresh evidence, a previous ``satisfied`` result carries forward if the target body and the spec are
unchanged, and the status is ``stale`` if either changed.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import jsonschema
import yaml

from coverset.analysis.functions import MODULE_QUALNAME, FunctionRegion, is_stub
from coverset.analysis.shapes import FunctionInputCov, InputClass

__all__ = [
    "INVARIANTS_SCHEMA",
    "Invariant",
    "InvariantChange",
    "InvariantContext",
    "InvariantError",
    "InvariantLens",
    "InvariantResult",
    "PreviousResult",
    "ReviewEvidence",
    "Scope",
    "VerifySpec",
    "diff_invariants",
    "evaluate",
    "inv_key",
    "invariant_lens",
    "load_invariants",
    "resolve_target",
    "spec_hash",
]

type InvariantStatus = Literal["satisfied", "violated", "untested", "stale", "needs_review"]
type ChangeKind = Literal["added", "removed", "weakened", "changed", "unchanged"]

MAX_BYTES: Final = 1024 * 1024
MAX_CHECK_CHARS: Final = 2000
SEVERITY_RANK: Final = {"info": 0, "warning": 1, "error": 2}
_FAILED: Final = frozenset({"failed", "error"})
_KNOWN_VIOLATION: Final = "xfailed"
_RAN: Final = frozenset({"passed", "xpassed", _KNOWN_VIOLATION, *_FAILED})
_STRING: Final = {"type": "string", "minLength": 1, "maxLength": 4000}

INVARIANTS_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "coverset invariants.yaml",
    "type": "object",
    "additionalProperties": False,
    "required": ["invariants"],
    "properties": {
        "version": {"const": 1},
        "invariants": {
            "type": "array",
            "maxItems": 1000,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "scope", "statement", "verify"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_.-]{0,127}$"},
                    "scope": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "target"],
                        "properties": {
                            "kind": {"enum": ["function", "class", "module", "package", "endpoint"]},
                            "target": {"type": "string", "minLength": 1, "maxLength": 512},
                        },
                    },
                    "statement": _STRING,
                    "severity": {"enum": ["error", "warning", "info"]},
                    "verify": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind"],
                            "properties": {
                                "kind": {"enum": ["test", "property", "runtime", "review"]},
                                "marker": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_.-]{0,127}$"},
                                "test_ids": {
                                    "type": "array",
                                    "maxItems": 256,
                                    "items": {"type": "string", "minLength": 1, "maxLength": 512},
                                },
                                "check": {"type": "string", "minLength": 1, "maxLength": MAX_CHECK_CHARS},
                            },
                        },
                    },
                    "examples": {"type": "array", "maxItems": 100, "items": _STRING},
                    "counterexamples": {"type": "array", "maxItems": 100, "items": _STRING},
                    "rationale": _STRING,
                    "owner": {"type": "string", "minLength": 1, "maxLength": 255},
                },
            },
        },
    },
}
"""JSON Schema (draft 2020-12) of ``invariants.yaml``; a bare list of invariants is also accepted."""


class InvariantError(ValueError):
    """``invariants.yaml`` is too large, not safe YAML, or does not match the schema."""


@dataclass(frozen=True, slots=True)
class Scope:
    """What an invariant is about: ``kind`` and a dotted ``target`` (or a route for endpoints)."""

    kind: Literal["function", "class", "module", "package", "endpoint"]
    target: str


@dataclass(frozen=True, slots=True)
class VerifySpec:
    """One way an invariant is verified."""

    kind: Literal["test", "property", "runtime", "review"]
    marker: str | None = None
    test_ids: tuple[str, ...] = ()
    check: str | None = None
    """A Python expression, syntax-checked only and never evaluated by the control plane."""


@dataclass(frozen=True, slots=True)
class Invariant:
    """One validated invariant; ``spec`` is its normalised JSON form (stored as ``spec_json``)."""

    id: str
    scope: Scope
    statement: str
    severity: Literal["error", "warning", "info"]
    verify: tuple[VerifySpec, ...]
    examples: tuple[str, ...] = ()
    counterexamples: tuple[str, ...] = ()
    rationale: str | None = None
    owner: str | None = None
    spec: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class PreviousResult:
    """The most recent stored result of the same invariant that is not ``stale`` (the last real
    evidence), for carry-forward and staleness. ``stale`` rows are skipped because they are derived:
    a run after a stale one compares against the last ``satisfied`` result again, so ``stale``
    persists until fresh evidence arrives instead of decaying to ``untested``."""

    run_id: str
    status: InvariantStatus
    target_body_hash: str | None
    spec_hash: str


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    """An advisory review finding about an invariant for one target body."""

    body_hash: str
    status: str
    """What the reviewer concluded, e.g. ``satisfied``, ``violated`` or ``abstain``."""
    evidence: str
    source: str


@dataclass(frozen=True, slots=True)
class InvariantContext:
    """Run facts :func:`evaluate` needs: outcomes, marker links, Hypothesis flags, target state."""

    results: Mapping[str, str]
    """Outcome by nodeid for the tests of this run (``not_run`` or absent when not executed)."""
    links: Mapping[str, Collection[str]]
    """Invariant ids (and marker values) each nodeid is linked to by ``@pytest.mark.invariant``."""
    hypothesis: Mapping[str, bool] = field(default_factory=dict)
    target_region: FunctionRegion | None = None
    target_body_hash: str | None = None
    previous_result: PreviousResult | None = None
    review_finding: ReviewEvidence | None = None

    @property
    def body_hash(self) -> str | None:
        """The target's current body hash (explicit, else from ``target_region``)."""
        if self.target_body_hash is not None:
            return self.target_body_hash
        return self.target_region.body_hash if self.target_region else None


@dataclass(frozen=True, slots=True)
class InvariantResult:
    """Status of one invariant in one run, with its evidence and the tests that decided it."""

    status: InvariantStatus
    evidence: dict[str, Any]
    covering_test_ids: list[str]


@dataclass(frozen=True, slots=True)
class InvariantChange:
    """How one invariant differs between the base and head of a PR."""

    inv_id: str
    kind: ChangeKind
    base: Invariant | None
    head: Invariant | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InvariantLens:
    """The coverage lens of one invariant: its tests and which input classes they reached."""

    inv_id: str
    tests: list[str]
    covers_changed_lines: bool | None
    reached: list[str]
    unreached: list[InputClass]
    summary: str


# --- loading -----------------------------------------------------------------------------------


class _NoAliasSafeLoader(yaml.SafeLoader):
    """A safe loader that refuses YAML aliases (alias bombs) as well as any non-standard tag."""

    def compose_node(self, parent: yaml.Node | None, index: object) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(None, None, "aliases are not allowed", event.start_mark)
        return super().compose_node(parent, index)


def load_invariants(text: str, *, max_bytes: int = MAX_BYTES) -> list[Invariant]:
    """Parse and validate ``invariants.yaml`` text; raise :class:`InvariantError` on any problem."""
    if len(text.encode("utf-8", "surrogatepass")) > max_bytes:
        raise InvariantError(f"invariants file exceeds {max_bytes} bytes")
    try:
        data = yaml.load(text, Loader=_NoAliasSafeLoader)  # a SafeLoader subclass: no arbitrary tags
    except (yaml.YAMLError, RecursionError) as exc:
        raise InvariantError(f"invalid YAML: {_first_line(exc)}") from exc
    if data is None:
        return []
    if isinstance(data, list):
        data = {"invariants": data}
    validator = jsonschema.Draft202012Validator(INVARIANTS_SCHEMA)
    error = jsonschema.exceptions.best_match(validator.iter_errors(data))
    if error is not None:
        where = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise InvariantError(f"schema violation at {where}: {error.message[:300]}")
    invariants: list[Invariant] = []
    seen: set[str] = set()
    for raw in data["invariants"]:
        inv = _build(raw)
        if inv.id in seen:
            raise InvariantError(f"duplicate invariant id {inv.id!r}")
        seen.add(inv.id)
        invariants.append(inv)
    return invariants


def _build(raw: Mapping[str, Any]) -> Invariant:
    verify = []
    for entry in raw["verify"]:
        check = entry.get("check")
        if check is not None:
            _syntax_check(raw["id"], check)
        verify.append(VerifySpec(entry["kind"], entry.get("marker"), tuple(entry.get("test_ids", ())), check))
    spec = {
        "id": raw["id"],
        "scope": {"kind": raw["scope"]["kind"], "target": raw["scope"]["target"]},
        "statement": raw["statement"],
        "severity": raw.get("severity", "error"),
        "verify": [
            {
                k: v
                for k, v in (
                    ("kind", e.kind),
                    ("marker", e.marker),
                    ("test_ids", list(e.test_ids)),
                    ("check", e.check),
                )
                if v
            }
            for e in verify
        ],
        "examples": list(raw.get("examples", ())),
        "counterexamples": list(raw.get("counterexamples", ())),
        "rationale": raw.get("rationale"),
        "owner": raw.get("owner"),
    }
    return Invariant(
        id=raw["id"],
        scope=Scope(raw["scope"]["kind"], raw["scope"]["target"]),
        statement=raw["statement"],
        severity=spec["severity"],
        verify=tuple(verify),
        examples=tuple(spec["examples"]),
        counterexamples=tuple(spec["counterexamples"]),
        rationale=spec["rationale"],
        owner=spec["owner"],
        spec=spec,
    )


def _syntax_check(inv_id: str, expr: str) -> None:
    """Reject a ``check`` that is not a single Python expression. Never evaluates it."""
    try:
        ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise InvariantError(f"invariant {inv_id!r}: check is not a valid expression") from exc


def inv_key(inv: Invariant) -> str:
    """The stable identity of an invariant across spec versions (its id)."""
    return inv.id


def spec_hash(inv: Invariant) -> str:
    """sha256 of the canonical JSON of the normalised spec (a new hash = a new spec version)."""
    canonical = json.dumps(inv.spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def resolve_target(
    inv: Invariant, regions: Sequence[FunctionRegion], source_root: str = ""
) -> FunctionRegion | None:
    """The region a function/class/module invariant targets, by dotted ``module.qualname``.

    When several regions share the qualname (``@overload`` stubs, a property's getter and setter,
    redefinitions), the first one with a real body wins: an ``@overload`` stub's ``...`` body
    (:func:`~coverset.analysis.functions.is_stub`) is never the target while the implementation
    exists, so the body hash, covering tests and coverage lens are the implementation's.
    """
    if inv.scope.kind not in ("function", "class", "module"):
        return None
    matches: list[FunctionRegion] = []
    for region in regions:
        module = _module_name(region.path, source_root)
        if module is None:
            continue
        if inv.scope.kind == "module":
            if region.qualname == MODULE_QUALNAME and module == inv.scope.target:
                return region
        elif region.kind != "module" and f"{module}.{region.qualname}" == inv.scope.target:
            wanted_class = inv.scope.kind == "class"
            if (region.kind == "class") == wanted_class:
                matches.append(region)
    return next((r for r in matches if not is_stub(r)), matches[0] if matches else None)


def _module_name(path: str, source_root: str) -> str | None:
    root = source_root.strip("/")
    if root and root != ".":
        if not path.startswith(root + "/"):
            return None
        path = path[len(root) + 1 :]
    if not path.endswith(".py"):
        return None
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# --- evaluation --------------------------------------------------------------------------------


def evaluate(inv: Invariant, ctx: InvariantContext) -> InvariantResult:
    """Status of ``inv`` in one run (see the module docstring for the rules)."""
    entries: list[dict[str, Any]] = []
    decided: list[str] = []
    review_statuses: list[str] = []
    covering: set[str] = set()
    for spec in inv.verify:
        if spec.kind in ("test", "property"):
            entry, ran = _evaluate_tests(inv, spec, ctx)
            decided.append(entry["status"])
            covering |= ran
        elif spec.kind == "runtime":
            entry = {"kind": "runtime", "status": "untested", "reason": "runtime checks not implemented"}
            decided.append("untested")
        else:
            entry = _evaluate_review(ctx)
            review_statuses.append(entry["status"])
        entries.append(entry)
    if "violated" in decided:
        status: InvariantStatus = "violated"
    elif "satisfied" in decided:
        status = "satisfied"
    elif decided:
        status = "untested"
    else:
        status = (
            "satisfied"
            if review_statuses and all(s == "satisfied" for s in review_statuses)
            else "needs_review"
        )
    evidence: dict[str, Any] = {"entries": entries}
    reviewed = any("finding" in e for e in entries)
    if status == "untested" or (status == "needs_review" and not reviewed):
        status, evidence = _carry_forward(inv, ctx, status, evidence)
    return InvariantResult(status, evidence, sorted(covering))


def _evaluate_tests(
    inv: Invariant, spec: VerifySpec, ctx: InvariantContext
) -> tuple[dict[str, Any], set[str]]:
    linked = _linked_tests(inv, spec, ctx)
    if spec.kind == "property":
        linked = {n for n in linked if ctx.hypothesis.get(n, False)}
    ran = {n for n in linked if ctx.results.get(n) in _RAN}
    failed = sorted(n for n in ran if ctx.results[n] in _FAILED)
    xfailed = sorted(n for n in ran if ctx.results[n] == _KNOWN_VIOLATION)
    entry: dict[str, Any] = {"kind": spec.kind, "linked": len(linked), "ran": len(ran), "failed": failed}
    if xfailed:
        entry["xfailed"] = xfailed
    if failed or xfailed:
        entry["status"] = "violated"
        if not failed:
            entry["reason"] = "a linked test is marked xfail (a known violation)"
    elif ran:
        entry["status"] = "satisfied"
    else:
        entry["status"] = "untested"
        entry["reason"] = "no linked Hypothesis test ran" if spec.kind == "property" else "no linked test ran"
    return entry, ran


def _linked_tests(inv: Invariant, spec: VerifySpec, ctx: InvariantContext) -> set[str]:
    nodeids = set(ctx.results) | set(ctx.links)
    if spec.test_ids:
        return {n for n in nodeids if any(_nodeid_matches(n, t) for t in spec.test_ids)}
    marker = spec.marker or inv.id
    return {n for n, ids in ctx.links.items() if marker in ids}


def _nodeid_matches(nodeid: str, wanted: str) -> bool:
    return nodeid == wanted or nodeid.startswith((wanted + "[", wanted + "::"))


def _evaluate_review(ctx: InvariantContext) -> dict[str, Any]:
    finding = ctx.review_finding
    body = ctx.body_hash
    if finding is None or body is None or finding.body_hash != body:
        return {"kind": "review", "status": "needs_review", "reason": "no review of the current body"}
    status = "satisfied" if finding.status == "satisfied" else "needs_review"
    return {
        "kind": "review",
        "status": status,
        "advisory": True,
        "finding": {"status": finding.status, "evidence": finding.evidence[:2000], "source": finding.source},
    }


def _carry_forward(
    inv: Invariant, ctx: InvariantContext, status: InvariantStatus, evidence: dict[str, Any]
) -> tuple[InvariantStatus, dict[str, Any]]:
    prev = ctx.previous_result
    if prev is not None and prev.status == "stale":  # still no fresh evidence: it stays stale
        return "stale", {**evidence, "stale_since_run": prev.run_id, "reason": "no fresh evidence since"}
    if prev is None or prev.status != "satisfied":
        return status, evidence
    same_body = prev.target_body_hash == ctx.body_hash
    same_spec = prev.spec_hash == spec_hash(inv)
    if same_body and same_spec:
        return "satisfied", {**evidence, "carried_from_run": prev.run_id}
    changed = [name for name, same in (("target body", same_body), ("spec", same_spec)) if not same]
    return "stale", {**evidence, "stale_since_run": prev.run_id, "reason": f"{' and '.join(changed)} changed"}


# --- PR diff and coverage lens -----------------------------------------------------------------


def diff_invariants(base: Sequence[Invariant], head: Sequence[Invariant]) -> list[InvariantChange]:
    """Compare base and head definitions: removed, weakened, changed, added or unchanged."""
    head_by_id = {inv.id: inv for inv in head}
    base_ids = {inv.id for inv in base}
    changes: list[InvariantChange] = []
    for old in base:
        new = head_by_id.get(old.id)
        if new is None:
            changes.append(InvariantChange(old.id, "removed", old, None, ("removed from invariants.yaml",)))
        elif spec_hash(old) == spec_hash(new):
            changes.append(InvariantChange(old.id, "unchanged", old, new))
        else:
            weaker = _weakening(old, new)
            kind: ChangeKind = "weakened" if weaker else "changed"
            changes.append(
                InvariantChange(old.id, kind, old, new, tuple(weaker) or _changed_fields(old, new))
            )
    changes += [InvariantChange(inv.id, "added", None, inv) for inv in head if inv.id not in base_ids]
    return changes


def _weakening(old: Invariant, new: Invariant) -> list[str]:
    reasons: list[str] = []
    if SEVERITY_RANK[new.severity] < SEVERITY_RANK[old.severity]:
        reasons.append(f"severity lowered from {old.severity} to {new.severity}")
    if new.scope != old.scope:
        reasons.append(
            f"scope changed from {old.scope.kind} {old.scope.target} to {new.scope.kind} {new.scope.target}"
        )
    for entry in old.verify:
        kept = any(
            e.kind == entry.kind
            and e.marker == entry.marker
            and e.check == entry.check
            and set(entry.test_ids) <= set(e.test_ids)
            for e in new.verify
        )
        if not kept:
            reasons.append(
                f"verify entry removed or narrowed: {entry.kind}"
                + (f" ({entry.marker})" if entry.marker else "")
            )
    return reasons


def _changed_fields(old: Invariant, new: Invariant) -> tuple[str, ...]:
    return tuple(f"{key} changed" for key in sorted(old.spec) if old.spec.get(key) != new.spec.get(key))


def invariant_lens(
    inv: Invariant,
    input_cov: FunctionInputCov | None,
    linked_tests: Collection[str],
    *,
    covers_changed_lines: bool | None = None,
) -> InvariantLens:
    """Linked tests, whether they cover the target's changed lines, and the input classes they reached."""
    tests = sorted(set(linked_tests))
    linked = set(tests)
    reached: list[str] = []
    unreached: list[InputClass] = []
    if input_cov is not None:
        for cls in input_cov.expected:
            if linked & set(input_cov.tests.get(cls.name, ())):
                reached.append(cls.name)
            else:
                unreached.append(cls)
    noun = "test" if len(tests) == 1 else "tests"
    summary = f"{inv.id}: {len(tests)} {noun}" if tests else f"{inv.id}: no linked tests"
    if input_cov is not None and input_cov.expected:
        if unreached:
            summary += ", never reached " + ", ".join(c.name for c in unreached)
        else:
            summary += f", reached all {len(reached)} input classes"
    if covers_changed_lines is False:
        summary += "; changed lines not covered by linked tests"
    return InvariantLens(inv.id, tests, covers_changed_lines, reached, unreached, summary)


def _first_line(exc: BaseException) -> str:
    return str(exc).strip().splitlines()[0][:300] if str(exc).strip() else type(exc).__name__
