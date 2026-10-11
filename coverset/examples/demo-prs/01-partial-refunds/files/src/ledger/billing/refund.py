"""Refunds of part or all of a paid invoice."""

from ledger.billing.allocate import allocate_cents


def refund(total_cents: int, weights: list[float]) -> list[int]:
    """Split a refund of ``total_cents`` across invoice lines in proportion to ``weights``.

    Refunds post to the ledger as negative amounts, so ``total_cents`` must be zero or
    negative and every returned share is too. Raises ValueError for a positive total.
    """
    if total_cents > 0:
        raise ValueError(f"refunds are negative amounts: {total_cents}")
    return allocate_cents(total_cents, weights)
