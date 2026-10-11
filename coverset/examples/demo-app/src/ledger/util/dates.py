"""Calendar helpers for billing periods and payment terms."""

import calendar
from datetime import date, timedelta


def is_business_day(day: date) -> bool:
    """Whether ``day`` falls on a weekday (public holidays are not modelled)."""
    return day.weekday() < 5


def add_business_days(start: date, days: int) -> date:
    """Return the date ``days`` business days after ``start``, skipping weekends."""
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if is_business_day(current):
            remaining -= 1
    return current


def month_end(day: date) -> date:
    """Return the last day of the month containing ``day``."""
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def quarter_of(day: date) -> int:
    """Return the calendar quarter (1-4) containing ``day``."""
    return (day.month - 1) // 3 + 1


def billing_period(day: date) -> tuple[date, date]:
    """Return the first and last day of the monthly billing period containing ``day``."""
    return day.replace(day=1), month_end(day)
