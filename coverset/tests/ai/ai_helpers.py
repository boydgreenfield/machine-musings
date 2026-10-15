"""Data builders for the AI-review tests (demo-app sources, shapes, invariants)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace

from coverset.ai.bundle import bundle_hash
from coverset.ai.schema import (
    CoveringTest,
    FunctionBundle,
    FunctionVerdict,
    InputCovSummary,
    MissingClassHint,
    ObservedShape,
    ReviewBundle,
    ReviewContext,
    ReviewResult,
)
from coverset.analysis.functions import index_functions
from coverset.analysis.invariants import Invariant, load_invariants
from coverset.lang.base import FunctionRegion, ShapeObs

PROJECT = Path(__file__).resolve().parents[2]
DEMO = PROJECT / "examples" / "demo-app"
DEMO_PRS = PROJECT / "examples" / "demo-prs"
EMAIL_PATH = "src/ledger/accounts/emailcheck.py"
ALLOCATE_PATH = "src/ledger/billing/allocate.py"
REFUND_PATH = "src/ledger/billing/refund.py"

EMAIL_TESTS: list[tuple[str, str]] = [
    (
        "tests/test_emailcheck.py::test_validate_email_returns_domain",
        'def test_validate_email_returns_domain():\n    assert validate_email("bob@acme.com") == "acme.com"\n',
    ),
    (
        "tests/test_emailcheck.py::test_validate_email_rejects_malformed",
        'def test_validate_email_rejects_malformed():\n    with pytest.raises(ValueError):\n        validate_email("frank@")\n',
    ),
]


def demo_source(path: str) -> str:
    """Head-side source of a demo-app file (PR files take precedence for files a PR adds)."""
    for root in (DEMO_PRS / "01-partial-refunds" / "files", DEMO):
        candidate = root / path
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(path)


def demo_invariants() -> list[Invariant]:
    return load_invariants((DEMO / "invariants.yaml").read_text(encoding="utf-8"))


def region_of(path: str, source: str, qualname: str) -> FunctionRegion:
    return next(r for r in index_functions(path, source) if r.qualname == qualname)


def obs(
    region: FunctionRegion,
    param: str,
    key: str,
    *,
    samples: int = 1,
    tests: Sequence[str] = (),
    example: str | None = None,
) -> ShapeObs:
    return ShapeObs(
        func=region.triple,
        param=param,
        shape_key=key,
        caller=tests[0] if tests else "tests/test_x.py::test_x",
        samples=samples,
        tests=tuple(tests),
        truncated=False,
        example=example,
        example_test=tests[0] if tests else None,
    )


def email_shapes(region: FunctionRegion) -> list[ShapeObs]:
    return [
        obs(
            region,
            "email",
            "str|ascii|kind:email/plain|len:11-100",
            samples=14,
            tests=[EMAIL_TESTS[0][0]],
            example="'bob@acme.com'",
        ),
        obs(region, "email", "str|ascii|len:2-10", samples=3, tests=[EMAIL_TESTS[1][0]], example="'frank@'"),
    ]


BundleFactory = Callable[..., ReviewBundle]
"""The ``email_bundle`` fixture: builds a ``validate_email`` bundle with keyword overrides."""


# --- hand-built bundles ---------------------------------------------------------------------------

DEFAULT_SOURCE = (
    'def scale(amount: int, factor: int) -> int:\n    """Multiply."""\n    return amount * factor\n'
)


def make_function(**overrides: object) -> FunctionBundle:
    """A small, fully specified function entry; any field can be overridden."""
    values: dict[str, object] = {
        "qualname": "scale",
        "func_key": "0123456789abcdef",
        "path": "src/pkg/maths.py",
        "module": "pkg.maths",
        "change_kind": "modified",
        "body_hash": "feedfacecafebeef",
        "params": ["amount", "factor"],
        "signature_context": "def scale(amount: int, factor: int) -> int:",
        "source": DEFAULT_SOURCE,
        "covering_test_count": 2,
        "covering_tests": [
            CoveringTest(
                nodeid="tests/test_maths.py::test_scale",
                body="def test_scale():\n    assert scale(2, 3) == 6\n",
            ),
            CoveringTest(
                nodeid="tests/test_maths.py::test_scale_zero",
                body="def test_scale_zero():\n    assert scale(0, 3) == 0\n",
            ),
        ],
        "observed_shapes": [
            ObservedShape(
                param="amount",
                shape_key="int|pos",
                samples=4,
                example="2",
                tests=["tests/test_maths.py::test_scale"],
            ),
            ObservedShape(
                param="factor",
                shape_key="int|pos",
                samples=4,
                example="3",
                tests=["tests/test_maths.py::test_scale"],
            ),
        ],
        "input_cov": InputCovSummary(
            expected=["amount:negative", "amount:zero", "amount:positive", "factor:positive"],
            observed=["amount:positive", "amount:zero", "factor:positive"],
            missing=[
                MissingClassHint(
                    name="amount:negative",
                    family="number",
                    description="a negative number",
                    hypothesis_hint="st.integers(max_value=-1)",
                )
            ],
            missing_reason="never exercised",
        ),
    }
    values.update(overrides)
    return FunctionBundle.model_validate(values)


def make_bundle(*functions: FunctionBundle, repo: str = "acme/maths") -> ReviewBundle:
    bundle = ReviewBundle(
        context=ReviewContext(repo=repo, head_sha="b" * 40, pr_number=7), functions=list(functions)
    )
    bundle.bundle_hash = bundle_hash(bundle)
    return bundle


def verdict(qualname: str = "scale", **overrides: object) -> FunctionVerdict:
    values: dict[str, object] = {
        "qualname": qualname,
        "verdict": "partially_covered",
        "confidence": "medium",
        "evidence": [{"test_id": "tests/test_maths.py::test_scale", "what_it_exercises": "positive amounts"}],
        "missing_input_classes": [
            {
                "name": "amount:negative",
                "description": "A negative amount.",
                "why_likely": "Refunds are negative.",
                "example_value": "-1",
                "severity": "high",
                "hypothesis_strategy": "st.integers(max_value=-1)",
            }
        ],
        "suggested_tests": [],
        "invariant_findings": [],
        "abstain_reason": "",
    }
    values.update(overrides)
    return FunctionVerdict.model_validate(values)


def missing(name: str, severity: str = "medium") -> dict[str, str]:
    return {
        "name": name,
        "description": f"Class {name}.",
        "why_likely": "Plausible.",
        "example_value": "0",
        "severity": severity,
        "hypothesis_strategy": "st.integers()",
    }


# --- a stub of the Anthropic SDK surface the client uses -------------------------------------------


def response(
    parsed: ReviewResult | None = None,
    *,
    stop_reason: str = "end_turn",
    category: str | None = None,
    model: str = "claude-opus-5-5",
    input_tokens: int = 1_000,
    output_tokens: int = 500,
    cache_read: int = 2_000,
    cache_write: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(type="refusal", category=category) if stop_reason == "refusal" else None,
        model=model,
        parsed_output=parsed,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


class StubMessages:
    """``beta.messages`` with a ``parse`` that records kwargs and replays queued replies."""

    def __init__(self, replies: Sequence[object]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(kwargs)
        return reply


class StubAnthropic:
    """Stands in for ``anthropic.Anthropic``: only ``beta.messages.parse`` exists."""

    def __init__(self, *replies: object) -> None:
        self.beta = SimpleNamespace(messages=StubMessages(replies))

    @property
    def calls(self) -> list[dict[str, object]]:
        return self.beta.messages.calls
