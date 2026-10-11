"""Tests for ledger.accounts.roles."""

import pytest

from ledger.accounts.roles import Role, can_invite, highest_role, parse_role


@pytest.mark.parametrize(("role", "allowed"), [(Role.MEMBER, False), (Role.ADMIN, True)])
def test_can_invite(role: Role, allowed: bool) -> None:
    assert can_invite(role) is allowed


def test_highest_role() -> None:
    assert highest_role([Role.MEMBER, Role.OWNER, Role.VIEWER]) is Role.OWNER
    assert highest_role([]) is Role.VIEWER


def test_parse_role_ignores_case_and_spaces() -> None:
    assert parse_role(" Admin ") is Role.ADMIN


def test_parse_role_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        parse_role("superuser")
