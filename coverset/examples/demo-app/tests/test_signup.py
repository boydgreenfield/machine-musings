"""Tests for ledger.accounts.signup."""

import time

import pytest

from ledger.accounts.signup import check_if_new_user_signup_allowed

CUSTOMER_DOMAINS = frozenset({"acme.com", "globex.io"})


def test_new_domain_may_sign_up() -> None:
    assert check_if_new_user_signup_allowed("alice@example.com", CUSTOMER_DOMAINS)


def test_plus_tagged_address_may_sign_up() -> None:
    assert check_if_new_user_signup_allowed("ivan+trial@example.com", CUSTOMER_DOMAINS)


def test_existing_customer_domain_must_be_invited() -> None:
    assert not check_if_new_user_signup_allowed("dana@acme.com", CUSTOMER_DOMAINS)


def test_existing_domain_match_ignores_case() -> None:
    assert not check_if_new_user_signup_allowed("Erin@Globex.IO", CUSTOMER_DOMAINS)


def test_anyone_may_sign_up_before_the_first_customer() -> None:
    assert check_if_new_user_signup_allowed("dana@acme.com", frozenset())


def test_single_customer_domain_must_be_invited() -> None:
    assert not check_if_new_user_signup_allowed("dana@acme.com", frozenset({"acme.com"}))


@pytest.mark.parametrize("email", ["frank@", "", " ", " dana@acme.com", "Zoë Smith <zoe@example.com>"])
def test_malformed_email_is_an_error(email: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        check_if_new_user_signup_allowed(email, CUSTOMER_DOMAINS)


def test_bulk_signup_screening_slow() -> None:
    """Deliberately slow (0.3 s) so the sharding demo has uneven test durations."""
    time.sleep(0.3)
    emails = [f"user{n}@example.com" for n in range(20)]
    assert all(check_if_new_user_signup_allowed(email, CUSTOMER_DOMAINS) for email in emails)
