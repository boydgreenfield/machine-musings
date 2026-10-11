"""Tests for ledger.accounts.emailcheck."""

import pytest

from ledger.accounts.emailcheck import validate_email


@pytest.mark.parametrize(
    ("email", "domain"),
    [
        ("alice@example.com", "example.com"),
        ("Bob.Smith+billing@Example.ORG", "example.org"),
    ],
)
def test_validate_email_returns_the_domain(email: str, domain: str) -> None:
    assert validate_email(email) == domain


@pytest.mark.parametrize(
    "email",
    [
        "alice",
        "alice@",
        "@example.com",
        "alice smith@example.com",
        "",
        " ",
        " alice@example.com",
        "Zoë Smith <zoe@example.com>",
    ],
)
def test_validate_email_rejects_malformed(email: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        validate_email(email)
