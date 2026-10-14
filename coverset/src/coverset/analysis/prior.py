"""Choosing the prior run whose facts drive scoring, wave 1 and the inherited label.

The prior run is the most recent completed run whose head is (a) the PR's previous head, else
(b) the merge-base of base and head, else (c) the latest baseline run on the default branch.
Facts are read through the :class:`~coverset.lang.base.PriorRun` protocol.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from coverset.lang.base import PriorRun

__all__ = ["PriorCandidate", "PriorChoice", "PriorRun", "select_prior_run"]

type PriorBasis = Literal["previous_head", "merge_base", "baseline"]


@dataclass(frozen=True, slots=True)
class PriorCandidate:
    """A completed run that could serve as the prior run."""

    run_id: str
    head_sha: str
    branch: str | None
    created_at: datetime
    baseline: bool
    """True for a full instrumented run on the default branch."""


@dataclass(frozen=True, slots=True)
class PriorChoice:
    """The chosen prior run and the rule that chose it (recorded as ``scoring_basis_run_id``)."""

    run_id: str
    basis: PriorBasis

    def describe(self) -> str:
        """A short human label, e.g. ``"run 41 (PR's previous head)"``."""
        label = {"previous_head": "PR's previous head", "merge_base": "merge-base", "baseline": "baseline"}
        return f"run {self.run_id} ({label[self.basis]})"


def select_prior_run(
    candidates: Iterable[PriorCandidate],
    *,
    previous_head: str | None,
    merge_base: str | None,
    default_branch: str,
) -> PriorChoice | None:
    """Return the prior run by the (a) previous head, (b) merge-base, (c) baseline rule, or None."""
    newest_first = sorted(candidates, key=lambda c: (c.created_at, c.run_id), reverse=True)
    if previous_head:
        for cand in newest_first:
            if cand.head_sha == previous_head:
                return PriorChoice(cand.run_id, "previous_head")
    if merge_base:
        for cand in newest_first:
            if cand.head_sha == merge_base:
                return PriorChoice(cand.run_id, "merge_base")
    for cand in newest_first:
        if cand.baseline and cand.branch == default_branch:
            return PriorChoice(cand.run_id, "baseline")
    return None
