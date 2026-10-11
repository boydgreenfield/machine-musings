"""Email address validation shared by signup and invitations."""

import re

_VENDOR_PATTERN = re.compile(r"[A-Za-z0-9._+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")


def validate_email(email: str) -> str:
    """Check ``email`` and return its domain, lowercased.

    A thin wrapper around our email-delivery vendor's address check: the pattern mirrors
    the syntax rule the vendor applies before accepting a recipient, including domains
    with several labels, so a signup we accept is one the vendor will deliver to.
    Raises ValueError for a malformed address.
    """
    match = _VENDOR_PATTERN.fullmatch(email)
    if match is None:
        raise ValueError(f"malformed email address: {email!r}")
    return match.group(1).lower()
