"""Pydantic models for AI review: the reviewer's input (:class:`ReviewBundle`) and output.

The output models (:class:`ReviewResult` and everything under it) are the structured-output schema
passed to ``client.messages.parse(..., output_format=ReviewResult)``. They follow the structured
output rules: no recursion, enums (``Literal``) instead of numbers, no numeric or length
constraints, every field required, ``additionalProperties: false`` (``extra="forbid"``), and field
descriptions that tell the model what to write. Rationale fields are short and never called
"reasoning".

The input models describe one review bundle: run-level context plus one entry per changed function,
built only from redacted facts by :func:`coverset.ai.bundle.build_review_bundle`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "BUNDLE_VERSION",
    "Confidence",
    "CoveringTest",
    "FindingStatus",
    "Evidence",
    "FunctionBundle",
    "FunctionVerdict",
    "InputCovSummary",
    "InvariantFinding",
    "InvariantInScope",
    "MissingClassHint",
    "MissingInputClass",
    "ObservedShape",
    "ReviewBundle",
    "ReviewContext",
    "ReviewResult",
    "Severity",
    "SuggestedTest",
    "TestStyle",
    "Verdict",
]

BUNDLE_VERSION = 1

# Plain aliases (not PEP 695 ``type`` statements) so the JSON schema inlines each enum next to its
# field description instead of moving it behind a ``$ref`` that drops the description.
Verdict = Literal["covered", "partially_covered", "not_covered", "abstain"]
Confidence = Literal["low", "medium", "high"]
Severity = Literal["high", "medium", "low"]
TestStyle = Literal["example", "hypothesis", "stateful"]
FindingStatus = Literal["satisfied", "violated", "untested", "not_applicable"]

_STRICT = ConfigDict(extra="forbid")


# --- output: the structured-output schema ------------------------------------------------------


class Evidence(BaseModel):
    """One covering test and what it shows about the function."""

    model_config = _STRICT

    test_id: str = Field(description="The pytest nodeid of a covering test, copied from the bundle.")
    what_it_exercises: str = Field(
        description="One sentence: which inputs or behaviour of the function this test exercises."
    )


class MissingInputClass(BaseModel):
    """A block of a parameter's input space that no covering test reaches."""

    model_config = _STRICT

    name: str = Field(
        description=(
            "The input class as <param>:<class>, e.g. email:multi-label-domain. Reuse a name from the "
            "bundle's missing input classes when one fits."
        )
    )
    description: str = Field(description="One sentence describing the inputs in this class.")
    why_likely: str = Field(
        description="One or two sentences: why real callers are likely to pass such inputs, or what breaks."
    )
    example_value: str = Field(
        description="A concrete Python literal in this class, e.g. 'bob@eng.acme.com'."
    )
    severity: Severity = Field(
        description="high: likely wrong result or crash; medium: plausible gap; low: cosmetic or unlikely."
    )
    hypothesis_strategy: str = Field(
        description="A Hypothesis strategy expression that generates this class, e.g. st.emails()."
    )


class SuggestedTest(BaseModel):
    """A test the reviewer proposes adding."""

    model_config = _STRICT

    name: str = Field(description="A pytest function name starting with test_.")
    style: TestStyle = Field(
        description="example: a plain pytest example; hypothesis: an @given property; stateful: a rule machine."
    )
    code: str = Field(description="Complete Python source of the test, including the imports it needs.")


class InvariantFinding(BaseModel):
    """What the reviewer concluded about one invariant listed in the bundle."""

    model_config = _STRICT

    invariant_id: str = Field(description="The id of an invariant listed in the bundle.")
    status: FindingStatus = Field(
        description=(
            "satisfied: the code and tests uphold it; violated: the code breaks it for some input; "
            "untested: no covering test checks it; not_applicable: it does not concern this function."
        )
    )
    evidence: str = Field(description="One or two sentences citing a test id, an input or a code path.")


class FunctionVerdict(BaseModel):
    """The verdict on one changed function."""

    model_config = _STRICT

    qualname: str = Field(description="The function's qualname, copied exactly from the bundle.")
    verdict: Verdict = Field(
        description=(
            "covered: the tests exercise the meaningful input classes and assert on results; "
            "partially_covered: tests run the code but miss named input classes or assert weakly; "
            "not_covered: no test meaningfully exercises it (name at least one missing input class); "
            "abstain: the behaviour cannot be inferred from the bundle."
        )
    )
    confidence: Confidence = Field(description="How sure you are of the verdict.")
    evidence: list[Evidence] = Field(description="Up to five covering tests that support the verdict.")
    missing_input_classes: list[MissingInputClass] = Field(
        description="Input classes no covering test reaches, most severe first; empty when none."
    )
    suggested_tests: list[SuggestedTest] = Field(
        description="Up to three tests that would cover the missing classes; empty when covered."
    )
    invariant_findings: list[InvariantFinding] = Field(
        description="One entry per invariant listed for this function in the bundle."
    )
    abstain_reason: str = Field(
        description="When the verdict is abstain, one sentence saying what is missing; otherwise empty."
    )


class ReviewResult(BaseModel):
    """The structured output of one review request: one verdict per function in the bundle."""

    model_config = _STRICT

    functions: list[FunctionVerdict] = Field(description="One verdict per function in the bundle.")


# --- input: the review bundle ------------------------------------------------------------------


class CoveringTest(BaseModel):
    """A test that executed the function's body, with its (redacted, possibly truncated) source."""

    model_config = _STRICT

    nodeid: str
    body: str = ""
    """Redacted test source; empty when the body was not available."""


class ObservedShape(BaseModel):
    """One argument shape recorded by the shape tracer while the tests ran."""

    model_config = _STRICT

    param: str
    shape_key: str
    samples: int
    example: str | None = None
    """A redacted repr of one value of this shape."""
    tests: list[str] = Field(default_factory=list)
    """Up to three tests that passed a value of this shape."""


class MissingClassHint(BaseModel):
    """A deterministic missing input class from the catalogue, with its Hypothesis hint."""

    model_config = _STRICT

    name: str
    family: str
    description: str
    hypothesis_hint: str


class InputCovSummary(BaseModel):
    """Input-class coverage of the function as computed by ``analysis.shapes``."""

    model_config = _STRICT

    expected: list[str]
    observed: list[str]
    missing: list[MissingClassHint]
    missing_reason: str
    """How missing classes are labelled: "never exercised" or a sampling note."""
    unannotated: list[str] = Field(default_factory=list)
    """Notes such as ``email unannotated: observed {str}`` (never reported as 100%)."""
    pct: float | None = None


class InvariantInScope(BaseModel):
    """An invariant whose target is the function itself or a function it calls."""

    model_config = _STRICT

    id: str
    statement: str
    kind: str
    """Scope kind of the invariant: function, class, module, package or endpoint."""
    target: str
    severity: Literal["error", "warning", "info"]
    verify: list[str]
    """Verify kinds: test, property, runtime, review."""
    relation: Literal["target", "calls_target"] = "target"
    linked_tests: list[str] = Field(default_factory=list)
    """Covering tests linked to the invariant by marker or ``test_ids``."""


class FunctionBundle(BaseModel):
    """Everything the reviewer sees about one changed function."""

    model_config = _STRICT

    qualname: str
    func_key: str
    path: str
    module: str | None = None
    change_kind: str
    body_hash: str
    params: list[str] = Field(default_factory=list)
    signature_context: str = ""
    source: str = ""
    """Post-change source of the function; empty when unavailable (the reviewer must abstain)."""
    diff_hunk: str = ""
    line_pct: float | None = None
    covering_test_count: int = 0
    """How many tests executed the body (may exceed ``len(covering_tests)`` after capping)."""
    covering_tests: list[CoveringTest] = Field(default_factory=list)
    observed_shapes: list[ObservedShape] = Field(default_factory=list)
    input_cov: InputCovSummary | None = None
    invariants: list[InvariantInScope] = Field(default_factory=list)
    oracle_notes: list[str] = Field(default_factory=list)
    """Lowest-priority hints about the test oracle (examples, assertion-free tests)."""

    @property
    def error_invariants(self) -> list[InvariantInScope]:
        """Invariants in scope with ``severity: error`` (these trigger self-consistency)."""
        return [inv for inv in self.invariants if inv.severity == "error"]


class ReviewContext(BaseModel):
    """Run-level context. ``head_sha`` and ``pr_number`` identify the run but are not hashed."""

    model_config = _STRICT

    repo: str
    head_sha: str | None = None
    pr_number: int | None = None
    language: str = "python"
    conventions: list[str] = Field(default_factory=list)


class ReviewBundle(BaseModel):
    """The redacted, size-capped input to a review."""

    model_config = _STRICT

    version: int = BUNDLE_VERSION
    context: ReviewContext
    functions: list[FunctionBundle] = Field(default_factory=list)
    dropped: dict[str, int] = Field(default_factory=dict)
    """What the builder left out or cut: excluded paths, dropped notes/tests/functions, cut chars."""
    bundle_hash: str = ""
    """sha256 of the canonical JSON of what the reviewer sees (see :func:`~coverset.ai.bundle.bundle_hash`)."""

    def function(self, func_key: str) -> FunctionBundle | None:
        """The function entry with ``func_key``, or None."""
        return next((fn for fn in self.functions if fn.func_key == func_key), None)

    def for_function(self, func_key: str) -> ReviewBundle:
        """A single-function bundle (same context) with its own hash: the unit of one review call."""
        from coverset.ai.bundle import bundle_hash  # local import: bundle imports this module

        fn = self.function(func_key)
        if fn is None:
            raise KeyError(func_key)
        sub = ReviewBundle(context=self.context.model_copy(deep=True), functions=[fn.model_copy(deep=True)])
        sub.bundle_hash = bundle_hash(sub)
        return sub
