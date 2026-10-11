"""Advisory AI review of changed functions behind a pluggable client.

``build_review_bundle`` turns a run's redacted facts into a :class:`ReviewBundle`;
``get_review_client`` picks the client for ``COVERSET_AI`` (off, fake or anthropic);
``review_changes`` reviews one function per call and returns :class:`ReviewFindingRow` objects for
the ``review_findings`` table. Findings are labelled fixture, heuristic or anthropic and never change
pass/fail or test-decided invariant statuses.
"""

from coverset.ai.bundle import build_review_bundle, bundle_hash, is_excluded_path
from coverset.ai.client import (
    ConfigError,
    FindingSource,
    ReviewClient,
    ReviewError,
    ReviewOutcome,
    get_review_client,
)
from coverset.ai.review import (
    ReviewFindingRow,
    findings_to_invariant_statuses,
    format_findings_markdown,
    review_changes,
    review_evidence,
)
from coverset.ai.schema import FunctionBundle, FunctionVerdict, ReviewBundle, ReviewContext, ReviewResult

__all__ = [
    "ConfigError",
    "FindingSource",
    "FunctionBundle",
    "FunctionVerdict",
    "ReviewBundle",
    "ReviewClient",
    "ReviewContext",
    "ReviewError",
    "ReviewFindingRow",
    "ReviewOutcome",
    "ReviewResult",
    "build_review_bundle",
    "bundle_hash",
    "findings_to_invariant_statuses",
    "format_findings_markdown",
    "get_review_client",
    "is_excluded_path",
    "review_changes",
    "review_evidence",
]
