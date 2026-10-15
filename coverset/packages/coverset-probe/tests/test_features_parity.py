"""The vendored feature code computes exactly the plugin's shape keys (runs where the plugin is installed).

Run with ``--with-editable ./packages/pytest-coverset`` added to the isolated command; the control
plane's ``tests/test_flows_cli.py`` runs the same comparison in the main environment on every run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
from coverset_probe import _features as vendored

from coverset_model.shape_keys import is_valid


@dataclass
class _Point:
    x: int


CORPUS: list[object] = [
    None, True, False, 0, 1, -1, 2**40, -(2**40), 0.0, -0.0, 1.5, -2.0, math.nan, math.inf, -math.inf,
    "", " ", "\t\n", "abc", "café", " x ", "42", "-3.5", "2026-10-13", "2026-10-13T10:00:00Z",
    "https://example.com/a?b=1", "bob@acme.com", "bob@eng.acme.com", "bob+news@acme.com", "Bob@Acme.COM",
    "bob@bücher.de", "a@b.co.uk", "5m", "1h30m", "100ms", "1.5h", "-5m", "5m!", "x" * 5000,
    b"", b"abc", [], [1], [3, 1, 2], [1, 1], [0, 0], [1, -1], [1.0, math.nan], [(1, 2), (5, 1)],
    (1, 2), ("a", "b"), {1, 2}, frozenset({"a"}), {}, {"a": 1}, {1: "a"}, [1, "a"], _Point(1), _Point,
    object(), type,
]  # fmt: skip


def test_vendored_keys_are_valid() -> None:
    for value in CORPUS:
        assert is_valid(vendored.shape_key(value)), value


def test_vendored_keys_match_the_plugin() -> None:
    plugin = pytest.importorskip("pytest_coverset.features")
    for value in CORPUS:
        assert vendored.shape_key(value) == plugin.shape_key(value), value
