"""The :class:`ReviewClient` protocol, the :class:`ReviewOutcome` wrapper, and client selection.

``COVERSET_AI`` picks the client: ``off`` (no review), ``fake`` (fixtures, then a deterministic
heuristic; the default) or ``anthropic`` (Claude through the Anthropic SDK; needs
``ANTHROPIC_API_KEY`` and a repo whose ``policy.ai_review`` setting is on). Every outcome records
where its verdicts came from (``fixture``, ``heuristic`` or ``anthropic``) so the UI can label them;
all of them are advisory.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from coverset.ai.schema import ReviewBundle, ReviewResult

if TYPE_CHECKING:
    from coverset.config import Settings

__all__ = [
    "ConfigError",
    "FindingSource",
    "ReviewClient",
    "ReviewError",
    "ReviewOutcome",
    "get_review_client",
]

FindingSource = Literal["fixture", "heuristic", "anthropic"]


class ConfigError(ValueError):
    """The AI review configuration cannot work (e.g. ``COVERSET_AI=anthropic`` without a key)."""


class ReviewError(RuntimeError):
    """A review could not be produced (e.g. the API kept failing after retries).

    The message never contains the bundle, the prompt or the API key.
    """


class ReviewOutcome(BaseModel):
    """A :class:`ReviewResult` plus where it came from and what it cost.

    ``source`` is ``fixture`` only when every verdict came from a stored fixture; a bundle reviewed
    partly by the heuristic is labelled ``heuristic``. Token counts are summed over every request
    made for the bundle (three per function under self-consistency).
    """

    model_config = ConfigDict(extra="forbid")

    result: ReviewResult
    source: FindingSource
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd_estimate: float = 0.0
    requests: int = 0
    notes: list[str] = Field(default_factory=list)
    """Short, non-sensitive remarks: stop reasons, self-consistency agreement, fallback model."""

    @property
    def total_tokens(self) -> int:
        """Input (uncached, cache read and cache write) plus output tokens."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens + self.output_tokens


@runtime_checkable
class ReviewClient(Protocol):
    """Reviews a bundle of changed functions; implementations must never raise on refusals.

    :func:`coverset.ai.review.review_changes` calls :meth:`review` once per function with a
    single-function bundle (``bundle.for_function(func_key)``); clients also accept larger bundles.
    Network or API failures raise :class:`ReviewError`.
    """

    name: str

    def review(self, bundle: ReviewBundle) -> ReviewOutcome:
        """Return one verdict per function in ``bundle`` with its source and usage."""
        ...


def get_review_client(
    settings: Settings, *, repo_enabled: bool = True, fixtures_dir: Path | None = None
) -> ReviewClient | None:
    """The client ``settings.ai`` asks for, or None when AI review is off.

    ``anthropic`` needs ``ANTHROPIC_API_KEY`` (else :class:`ConfigError`) and only applies to repos
    whose ``policy.ai_review`` is on: for any other repo (``repo_enabled=False``) the fake client is
    returned, so no code leaves the machine for a repo that did not opt in.
    """
    mode = settings.ai
    if mode == "off":
        return None
    if mode == "fake" or (mode == "anthropic" and not repo_enabled):
        from coverset.ai.fake import FakeReviewClient

        return FakeReviewClient(fixtures_dir) if fixtures_dir is not None else FakeReviewClient()
    if mode == "anthropic":
        key = settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise ConfigError("COVERSET_AI=anthropic requires ANTHROPIC_API_KEY to be set")
        from coverset.ai.anthropic_client import AnthropicReviewClient

        return AnthropicReviewClient(settings)
    raise ConfigError(f"unknown COVERSET_AI mode: {mode!r} (expected off, fake or anthropic)")
