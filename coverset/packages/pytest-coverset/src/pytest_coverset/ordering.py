"""Priority ordering of collected items from a ``{nodeid: score}`` file.

Items are sorted by ``(-max_score(module), module_path, -max_score(class), class_name, -score, nodeid)``:
the riskiest module runs first, and within it the riskiest class, so module- and class-scoped
fixtures are still set up once per group. Unknown nodeids score 0.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest


def load_scores(path: str | os.PathLike[str]) -> dict[str, float]:
    """Read a JSON object mapping nodeids to finite numeric scores.

    Non-numeric values, booleans, NaN and infinities (which Python's JSON parser accepts) are ignored:
    a NaN in the sort key would make comparisons inconsistent and split module groups.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise pytest.UsageError(f"--coverset-scores: {path} must contain a JSON object of nodeid -> score")
    scores: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, int | float) and not isinstance(value, bool):
            try:
                score = float(value)
            except OverflowError:
                continue
            if math.isfinite(score):
                scores[str(key)] = score
    return scores


def _group_keys(item: pytest.Item) -> tuple[str, str]:
    module = item.nodeid.split("::", 1)[0]
    cls = item.getparent(pytest.Class)
    return module, (cls.nodeid if cls is not None else "")


def priority_order(items: Sequence[pytest.Item], scores: Mapping[str, float]) -> list[pytest.Item]:
    """Return ``items`` in priority order (stable for equal keys)."""
    module_max: dict[str, float] = {}
    class_max: dict[tuple[str, str], float] = {}
    keyed = []
    for item in items:
        module, cls = _group_keys(item)
        score = scores.get(item.nodeid, 0.0)
        module_max[module] = max(module_max.get(module, score), score)
        class_max[(module, cls)] = max(class_max.get((module, cls), score), score)
        keyed.append((item, module, cls, score))
    keyed.sort(key=lambda r: (-module_max[r[1]], r[1], -class_max[(r[1], r[2])], r[2], -r[3], r[0].nodeid))
    return [r[0] for r in keyed]


class PriorityOrdering:
    """pytest plugin object that applies :func:`priority_order` after every other collection hook."""

    def __init__(self, scores: Mapping[str, float]) -> None:
        self.scores = scores

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        items[:] = priority_order(items, self.scores)
