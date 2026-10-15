"""Formatting, parsing and percentages of integer cent amounts."""

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def format_cents(cents: int, currency: str = "USD") -> str:
    """Format ``cents`` with a currency symbol and thousands separators, e.g. ``-$1,234.50``."""
    symbol = _SYMBOLS.get(currency, f"{currency} ")
    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), 100)
    return f"{sign}{symbol}{whole:,}.{fraction:02d}"


def parse_amount(text: str) -> int:
    """Parse a decimal amount such as ``"1,234.56"`` into cents, rounding half to even."""
    try:
        value = Decimal(text.strip().replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    if not value.is_finite():
        raise ValueError(f"not a finite amount: {text!r}")
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def percent_of(cents: int, basis_points: int) -> int:
    """Return ``basis_points`` ten-thousandths of ``cents``, rounded half to even."""
    share = Decimal(cents) * basis_points / 10_000
    return int(share.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
