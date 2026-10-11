"""Account roles and the permissions they grant."""

from collections.abc import Iterable
from enum import IntEnum


class Role(IntEnum):
    """Roles within a customer account, ordered by privilege."""

    VIEWER = 1
    MEMBER = 2
    ADMIN = 3
    OWNER = 4


def can_invite(role: Role) -> bool:
    """Whether ``role`` may invite new people into the account."""
    return role >= Role.ADMIN


def can_manage_billing(role: Role) -> bool:
    """Whether ``role`` may change payment details and see invoices."""
    return role is Role.OWNER


def highest_role(roles: Iterable[Role]) -> Role:
    """Return the most privileged of ``roles``, or VIEWER when there are none."""
    return max(roles, default=Role.VIEWER)


def parse_role(name: str) -> Role:
    """Parse a role name such as ``"admin"``, ignoring case and surrounding spaces."""
    try:
        return Role[name.strip().upper()]
    except KeyError as exc:
        raise ValueError(f"unknown role: {name!r}") from exc
