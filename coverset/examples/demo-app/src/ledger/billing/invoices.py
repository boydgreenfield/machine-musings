"""Invoices made of priced lines, with tax and proportional discounts."""

from dataclasses import dataclass, replace

from ledger.billing.allocate import allocate_cents
from ledger.billing.tax import tax_cents
from ledger.util.money import format_cents


@dataclass(frozen=True)
class InvoiceLine:
    """One priced line on an invoice."""

    description: str
    amount_cents: int


@dataclass(frozen=True)
class Invoice:
    """A customer's invoice for one billing region."""

    customer: str
    region: str
    lines: tuple[InvoiceLine, ...]

    @property
    def subtotal_cents(self) -> int:
        """Sum of the line amounts before tax."""
        return sum(line.amount_cents for line in self.lines)

    @property
    def tax_cents(self) -> int:
        """Tax due on the subtotal in the invoice's region."""
        return tax_cents(self.subtotal_cents, self.region)

    @property
    def total_cents(self) -> int:
        """Subtotal plus tax."""
        return self.subtotal_cents + self.tax_cents


def apply_discount(invoice: Invoice, discount_cents: int) -> Invoice:
    """Spread ``discount_cents`` over the lines in proportion to their amounts."""
    if discount_cents > invoice.subtotal_cents:
        raise ValueError("discount exceeds the invoice subtotal")
    shares = allocate_cents(discount_cents, [line.amount_cents for line in invoice.lines])
    lines = tuple(
        replace(line, amount_cents=line.amount_cents - share)
        for line, share in zip(invoice.lines, shares, strict=True)
    )
    return replace(invoice, lines=lines)


def render_invoice(invoice: Invoice) -> str:
    """Render ``invoice`` as fixed-width plain text."""
    rows = [f"Invoice for {invoice.customer}"]
    rows += [f"  {line.description:<30}{format_cents(line.amount_cents):>12}" for line in invoice.lines]
    rows.append(f"  {'Tax':<30}{format_cents(invoice.tax_cents):>12}")
    rows.append(f"  {'Total':<30}{format_cents(invoice.total_cents):>12}")
    return "\n".join(rows)
