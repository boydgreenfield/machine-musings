"""Tests for ledger.util.dates."""

from datetime import date

from ledger.util.dates import billing_period, is_business_day, month_end


def test_is_business_day() -> None:
    assert is_business_day(date(2026, 10, 2))
    assert not is_business_day(date(2026, 10, 3))


def test_month_end_in_a_leap_february() -> None:
    assert month_end(date(2028, 2, 10)) == date(2028, 2, 29)


def test_billing_period_spans_the_calendar_month() -> None:
    assert billing_period(date(2026, 11, 17)) == (date(2026, 11, 1), date(2026, 11, 30))
