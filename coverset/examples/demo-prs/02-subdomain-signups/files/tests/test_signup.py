"""Tests for ledger.accounts.signup."""

import time

import pytest

from ledger.accounts.signup import check_if_new_user_signup_allowed

CUSTOMER_DOMAINS = frozenset({"acme.com", "Globex.IO"})


@pytest.mark.parametrize(
    ("email", "existing_domains", "allowed"),
    [
        ("alice@example.com", CUSTOMER_DOMAINS, True),
        ("ivan+trial@example.com", CUSTOMER_DOMAINS, True),
        ("dana@acme.com", CUSTOMER_DOMAINS, False),
        ("Erin@Globex.IO", CUSTOMER_DOMAINS, False),
        ("grace@globex.io", CUSTOMER_DOMAINS, False),
        ("heidi@initech.net", CUSTOMER_DOMAINS, True),
        ("dana@acme.com", frozenset(), True),
        ("dana@acme.com", frozenset({"ACME.com"}), False),
    ],
)
def test_signup_allowed(email: str, existing_domains: frozenset[str], allowed: bool) -> None:
    assert check_if_new_user_signup_allowed(email, existing_domains) is allowed


@pytest.mark.parametrize("email", ["frank@", "", " ", " dana@acme.com", "Zoë Smith <zoe@example.com>"])
def test_malformed_email_is_an_error(email: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        check_if_new_user_signup_allowed(email, CUSTOMER_DOMAINS)


def test_bulk_signup_screening_slow() -> None:
    """Deliberately slow (0.3 s) so the sharding demo has uneven test durations."""
    time.sleep(0.3)
    emails = [f"user{n}@example.com" for n in range(20)]
    assert all(check_if_new_user_signup_allowed(email, CUSTOMER_DOMAINS) for email in emails)
