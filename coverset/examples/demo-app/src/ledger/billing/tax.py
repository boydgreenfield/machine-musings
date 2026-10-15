"""Regional sales tax on invoice subtotals."""

from ledger.util.money import percent_of

TAX_RATES_BP: dict[str, int] = {
    "US-CA": 725,
    "US-NY": 400,
    "US-OR": 0,
    "GB": 2_000,
    "DE": 1_900,
}


def rate_for(region: str) -> int:
    """Return the tax rate for ``region`` in basis points."""
    try:
        return TAX_RATES_BP[region]
    except KeyError as exc:
        raise ValueError(f"no tax rate for region {region!r}") from exc


def tax_cents(amount_cents: int, region: str) -> int:
    """Tax due on ``amount_cents`` in ``region``, rounded half to even."""
    return percent_of(amount_cents, rate_for(region))


def is_tax_exempt(region: str) -> bool:
    """Whether ``region`` charges no sales tax."""
    return rate_for(region) == 0
