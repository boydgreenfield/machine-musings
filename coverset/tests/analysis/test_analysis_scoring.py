"""Wording of the explanation for tests with no risk signal (score 0)."""

from __future__ import annotations

from coverset.analysis.scoring import TestHistory, _quiet_explanation


def test_quiet_explanation_shows_the_p50_in_milliseconds() -> None:
    assert _quiet_explanation(TestHistory(runs_n=3), 0.00026) == "no risk signal; p50 0.3 ms"
    assert _quiet_explanation(None, 0.042) == "no risk signal; p50 42 ms"
    assert _quiet_explanation(None, 1.44) == "no risk signal; p50 1.4 s"
    assert _quiet_explanation(None, None) == "no risk signal; duration unknown"


def test_quiet_explanation_names_an_old_fail_rate() -> None:
    text = _quiet_explanation(TestHistory(runs_n=10, fail_ewma=0.25), 0.002)
    assert text == "no recent risk signal; fail rate 25%; p50 2.0 ms"
