"""The offline reviewer: stored fixtures first, then a deterministic heuristic over the bundle's facts.

Fixture lookup for a function with qualname ``Q`` in repo ``owner/name``:
``<fixtures_dir>/owner__name/Q.json``, then ``<fixtures_dir>/name/Q.json``, then
``<fixtures_dir>/default/Q.json`` (``Q`` with characters outside ``[A-Za-z0-9._-]`` replaced by
``_``). A fixture is a stored :class:`~coverset.ai.client.ReviewOutcome` (``result.functions``
holds the verdict whose ``qualname`` is ``Q``) plus an optional ``applies_to: {"path": ...}`` guard
(the function's path must end with it) and a free-text ``description``. Fixture verdicts are
labelled ``fixture``.

The heuristic (labelled ``heuristic``) only reads structured facts, never the source text, test
bodies or comments, so nothing written in the pull request can steer it:

* no post-change source in the bundle: ``abstain``;
* no covering test: ``not_covered`` (missing classes: the catalogue's, else one per parameter);
* covering tests and missing input classes: ``partially_covered``;
* covering tests and every expected class observed: ``covered`` (confidence low when parameters are
  unannotated or no shape facts were recorded);
* missing classes come from the catalogue with its Hypothesis hints, suggested tests are
  ``@given`` skeletons built from those hints, and invariant findings are ``untested`` unless
  linked covering tests exist (then ``satisfied``, or ``untested`` while input classes are missing).
"""

from __future__ import annotations

import ast
import json
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from coverset.ai.client import ReviewOutcome
from coverset.ai.schema import (
    Evidence,
    FunctionBundle,
    FunctionVerdict,
    InvariantFinding,
    MissingClassHint,
    MissingInputClass,
    ReviewBundle,
    ReviewResult,
    Severity,
    SuggestedTest,
)

__all__ = ["DEFAULT_FIXTURES_DIR", "FakeReviewClient", "heuristic_verdict"]

log = logging.getLogger(__name__)

DEFAULT_FIXTURES_DIR: Final = Path(__file__).resolve().parents[3] / "fixtures" / "ai"
"""``<project>/fixtures/ai`` when running from a source checkout."""

_SAFE: Final = re.compile(r"[^A-Za-z0-9._-]")
_RISKY_FAMILIES: Final = frozenset({"number", "elements", "none", "pairs", "order", "email", "quantity"})
_SEVERITY_RANK: Final = {"low": 0, "medium": 1, "high": 2}
_SKELETON_CLASSES: Final = 3
_RECEIVERS: Final = frozenset({"self", "cls"})
_MAX_EVIDENCE: Final = 5
_EXAMPLES: Final = {
    "negative": "-1",
    "zero": "0",
    "positive": "1",
    "nan": 'float("nan")',
    "inf": 'float("inf")',
    "none": "None",
    "true": "True",
    "false": "False",
    "str-empty": '""',
    "empty": '""',
    "container-empty": "[]",
    "bytes-empty": 'b""',
    "all-zero": "[0, 0]",
    "nan-element": '[1.0, float("nan")]',
    "single": "[1]",
    "multiple": "[1, 2]",
}


def _safe_name(text: str) -> str:
    return _SAFE.sub("_", text)[:200] or "_"


class FakeReviewClient:
    """Fixtures by repo and qualname, else the deterministic heuristic. Makes no network calls."""

    name = "fake"

    def __init__(self, fixtures_dir: Path | str = DEFAULT_FIXTURES_DIR) -> None:
        self.fixtures_dir = Path(fixtures_dir)

    def review(self, bundle: ReviewBundle) -> ReviewOutcome:
        """One verdict per function: a fixture when one matches, else the heuristic."""
        verdicts: list[FunctionVerdict] = []
        all_fixtures = bool(bundle.functions)
        notes: list[str] = []
        for fn in bundle.functions:
            fixture = self.fixture_verdict(bundle.context.repo, fn)
            if fixture is not None:
                verdicts.append(fixture)
                notes.append(f"{fn.qualname}: fixture")
            else:
                verdicts.append(heuristic_verdict(fn))
                notes.append(f"{fn.qualname}: heuristic")
                all_fixtures = False
        return ReviewOutcome(
            result=ReviewResult(functions=verdicts),
            source="fixture" if all_fixtures else "heuristic",
            notes=notes,
        )

    def fixture_paths(self, repo: str, qualname: str) -> list[Path]:
        """Candidate fixture files, most specific first (never outside ``fixtures_dir``)."""
        dirs = [_safe_name(repo.replace("/", "__")), _safe_name(repo.rsplit("/", 1)[-1]), "default"]
        name = _safe_name(qualname) + ".json"
        root = self.fixtures_dir.resolve()
        paths: list[Path] = []
        for directory in dict.fromkeys(dirs):
            candidate = (root / directory / name).resolve()
            if candidate.is_relative_to(root):
                paths.append(candidate)
        return paths

    def fixture_verdict(self, repo: str, fn: FunctionBundle) -> FunctionVerdict | None:
        """The stored verdict for ``fn`` in ``repo``, or None (missing, mismatched or invalid)."""
        for path in self.fixture_paths(repo, fn.qualname):
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                guard = data.pop("applies_to", None) or {}
                data.pop("description", None)
                outcome = ReviewOutcome.model_validate(data)
            except (OSError, ValueError, ValidationError) as exc:
                log.warning("ignoring invalid AI fixture %s: %s", path.name, type(exc).__name__)
                continue
            wanted = guard.get("path") if isinstance(guard, dict) else None
            if wanted and not fn.path.endswith(str(wanted)):
                continue
            for verdict in outcome.result.functions:
                if verdict.qualname == fn.qualname:
                    return verdict
        return None


# --- the heuristic ----------------------------------------------------------------------------


def heuristic_verdict(fn: FunctionBundle) -> FunctionVerdict:
    """A deterministic verdict from the bundle's structured facts (see the module docstring)."""
    if not fn.source.strip():
        return FunctionVerdict(
            qualname=fn.qualname,
            verdict="abstain",
            confidence="low",
            evidence=[],
            missing_input_classes=[],
            suggested_tests=[],
            invariant_findings=_invariant_findings(fn, []),
            abstain_reason="The post-change source of the function is not in the bundle.",
        )
    hints = fn.input_cov.missing if fn.input_cov is not None else []
    has_error_invariant = bool(fn.error_invariants)
    missing = sorted(
        (_missing_class(h, fn, has_error_invariant) for h in hints), key=lambda m: -_SEVERITY_RANK[m.severity]
    )
    tested = fn.covering_test_count > 0 or bool(fn.covering_tests)
    if not tested:
        verdict = "not_covered"
        if not missing:
            missing = _untested_params(fn, has_error_invariant)
        confidence = "medium"
    elif missing:
        verdict = "partially_covered"
        confidence = "medium"
    else:
        verdict = "covered"
        unannotated = fn.input_cov.unannotated if fn.input_cov is not None else []
        confidence = "low" if fn.input_cov is None or unannotated or not fn.input_cov.expected else "medium"
    return FunctionVerdict(
        qualname=fn.qualname,
        verdict=verdict,
        confidence=confidence,
        evidence=_evidence(fn),
        missing_input_classes=missing,
        suggested_tests=[_skeleton(fn, missing[:_SKELETON_CLASSES])] if missing else [],
        invariant_findings=_invariant_findings(fn, missing),
        abstain_reason="",
    )


def _severity(hint_family: str, has_error_invariant: bool) -> Severity:
    if has_error_invariant:
        return "high"
    return "medium" if hint_family in _RISKY_FAMILIES else "low"


def _example_value(hint: MissingClassHint) -> str:
    _, _, cls = hint.name.partition(":")
    if "e.g. " in hint.description:
        return hint.description.split("e.g. ", 1)[1].strip().rstrip(".")
    if cls in _EXAMPLES:
        return _EXAMPLES[cls]
    return f"a value drawn from {hint.hypothesis_hint}"


def _missing_class(
    hint: MissingClassHint, fn: FunctionBundle, has_error_invariant: bool
) -> MissingInputClass:
    reason = fn.input_cov.missing_reason if fn.input_cov is not None else "never exercised"
    why = f"No covering test passed a value in this class ({reason}); the code may mishandle it."
    return MissingInputClass(
        name=hint.name,
        description=hint.description[:1].upper() + hint.description[1:] + ".",
        why_likely=why,
        example_value=_example_value(hint),
        severity=_severity(hint.family, has_error_invariant),
        hypothesis_strategy=hint.hypothesis_hint,
    )


def _annotations(fn: FunctionBundle) -> dict[str, str]:
    """Parameter annotations parsed from the signature context (empty when it does not parse)."""
    text = "\n".join(
        line.strip()
        for line in fn.signature_context.splitlines()
        if not line.strip().startswith(("class ", "..."))
    )
    text = "\n".join(line for line in text.splitlines() if not line.startswith("@"))
    try:
        tree = ast.parse(text + "\n    ...")
    except (SyntaxError, ValueError):
        return {}
    node = tree.body[0] if tree.body else None
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {}
    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    return {a.arg: ast.unparse(a.annotation) for a in args if a.annotation is not None}


def _call_params(fn: FunctionBundle) -> list[str]:
    params = [p.lstrip("*") for p in fn.params if not p.startswith("*")]
    if "." in fn.qualname and params and params[0] in _RECEIVERS:
        params = params[1:]
    return params


def _untested_params(fn: FunctionBundle, has_error_invariant: bool) -> list[MissingInputClass]:
    notes = _annotations(fn)
    classes: list[MissingInputClass] = []
    for param in _call_params(fn) or ["call"]:
        annotation = notes.get(param)
        strategy = f"st.from_type({annotation})" if annotation else "st.nothing()"
        classes.append(
            MissingInputClass(
                name=f"{param}:any-value",
                description=f"Any value for {param}: no test calls the function.",
                why_likely="The changed function has no covering test, so none of its behaviour is checked.",
                example_value=f"any {annotation}" if annotation else "any valid argument",
                severity="high" if has_error_invariant else "medium",
                hypothesis_strategy=strategy,
            )
        )
    return classes


def _evidence(fn: FunctionBundle) -> list[Evidence]:
    per_test: dict[str, list[str]] = {}
    for shape in fn.observed_shapes:
        for test in shape.tests:
            per_test.setdefault(test, []).append(f"{shape.param} as {shape.shape_key}")
    evidence: list[Evidence] = []
    for test in [t.nodeid for t in fn.covering_tests] + sorted(per_test):
        if len(evidence) >= _MAX_EVIDENCE or any(e.test_id == test for e in evidence):
            continue
        shapes = per_test.get(test)
        what = (
            "Passes " + "; ".join(shapes[:3]) + "."
            if shapes
            else "Executes the function body (no argument shapes recorded for this test)."
        )
        evidence.append(Evidence(test_id=test, what_it_exercises=what))
    return evidence


def _literal(example: str | None) -> str | None:
    if example is None:
        return None
    try:
        ast.literal_eval(example)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return example


def _strategy_for(
    param: str, fn: FunctionBundle, missing: Sequence[MissingInputClass], notes: dict[str, str]
) -> str:
    hints = list(dict.fromkeys(m.hypothesis_strategy for m in missing if m.name.split(":", 1)[0] == param))
    hints = [h for h in hints if h != "st.nothing()"]
    if len(hints) == 1:
        return hints[0]
    if hints:
        return "st.one_of(" + ", ".join(hints) + ")"
    example = next(
        (_literal(s.example) for s in fn.observed_shapes if s.param == param and _literal(s.example)), None
    )
    if example is not None:
        return f"st.just({example})"
    annotation = notes.get(param)
    return f"st.from_type({annotation})" if annotation else "st.nothing()"


def _skeleton(fn: FunctionBundle, missing: Sequence[MissingInputClass]) -> SuggestedTest:
    notes = _annotations(fn)
    params = _call_params(fn)
    func = fn.qualname.rsplit(".", 1)[-1]
    owner = fn.qualname.split(".", 1)[0] if "." in fn.qualname and "<locals>" not in fn.qualname else None
    imported = owner or func
    slug = (
        _safe_name(missing[0].name.split(":", 1)[-1]).replace("-", "_").replace(".", "_")
        if missing
        else "inputs"
    )
    name = f"test_{func}_{slug}"
    lines = ["from hypothesis import given, strategies as st", ""]
    if fn.module:
        lines += [f"from {fn.module} import {imported}", ""]
    lines.append("")
    given = ", ".join(f"{p}={_strategy_for(p, fn, missing, notes)}" for p in params)
    lines.append(f"@given({given})" if given else "@given(st.just(None))")
    signature = ", ".join(params) if params else "_"
    lines.append(f"def {name}({signature}):")
    lines.append("    # Missing input classes: " + ", ".join(m.name for m in missing))
    if "st.nothing()" in given:
        lines.append("    # TODO: replace st.nothing() with a strategy for the parameters without one")
    call_args = ", ".join(f"{p}={p}" for p in params)
    if owner:
        lines.append(f"    obj = {owner}(...)  # TODO: build a {owner}")
        lines.append(f"    result = obj.{func}({call_args})")
    else:
        lines.append(f"    result = {func}({call_args})")
    lines.append("    # TODO: assert what must hold for these inputs (an invariant in scope, if any)")
    return SuggestedTest(name=name, style="hypothesis", code="\n".join(lines) + "\n")


def _invariant_findings(fn: FunctionBundle, missing: Sequence[MissingInputClass]) -> list[InvariantFinding]:
    findings: list[InvariantFinding] = []
    for inv in fn.invariants:
        if not inv.linked_tests:
            status = "untested"
            evidence = "No covering test is linked to this invariant by marker or test_ids."
        elif missing:
            status = "untested"
            evidence = (
                f"{len(inv.linked_tests)} linked test(s) run, but no test reaches "
                + ", ".join(m.name for m in missing[:3])
                + "."
            )
        else:
            status = "satisfied"
            evidence = (
                f"{len(inv.linked_tests)} linked test(s) reach every observed input class; "
                "their outcomes decide the invariant."
            )
        findings.append(InvariantFinding(invariant_id=inv.id, status=status, evidence=evidence))
    return findings
