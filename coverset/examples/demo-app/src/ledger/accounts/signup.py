"""Who may create an account by self-signup."""

import logging
from collections.abc import Collection

from ledger.accounts.emailcheck import validate_email

logger = logging.getLogger(__name__)


def check_if_new_user_signup_allowed(email: str, existing_domains: Collection[str]) -> bool:
    """Whether ``email`` may self-sign-up instead of waiting for an invitation.

    People whose email domain already belongs to a customer must be invited by that
    customer's admin, so their seat and data land in the customer's account rather than
    in a stray personal one. Raises ValueError for a malformed address.
    """
    domain = validate_email(email)
    if domain in existing_domains:
        logger.info("self-signup refused: %s belongs to an existing customer", domain)
        return False
    return True
