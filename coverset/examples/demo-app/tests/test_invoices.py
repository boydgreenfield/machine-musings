"""Tests for ledger.billing.invoices."""

import time

import pytest

from ledger.billing.invoices import Invoice, InvoiceLine, apply_discount


@pytest.fixture(scope="module")
def lines() -> tuple[InvoiceLine, ...]:
    """Two priced lines shared by every invoice in this module."""
    return (InvoiceLine("Seats (10)", 3_000), InvoiceLine("Support plan", 1_000))


@pytest.fixture(scope="class")
def invoice(lines: tuple[InvoiceLine, ...]) -> Invoice:
    """One invoice for a German customer, built once for TestInvoice."""
    return Invoice(customer="Acme GmbH", region="DE", lines=lines)


class TestInvoice:
    """Totals and discounts on a two-line invoice."""

    def test_subtotal(self, invoice: Invoice) -> None:
        assert invoice.subtotal_cents == 4_000

    def test_tax(self, invoice: Invoice) -> None:
        assert invoice.tax_cents == 760

    def test_total(self, invoice: Invoice) -> None:
        assert invoice.total_cents == 4_760

    def test_discount_is_spread_in_proportion(self, invoice: Invoice) -> None:
        discounted = apply_discount(invoice, 400)
        assert [line.amount_cents for line in discounted.lines] == [2_700, 900]

    def test_discount_cannot_exceed_subtotal(self, invoice: Invoice) -> None:
        with pytest.raises(ValueError, match="exceeds"):
            apply_discount(invoice, 4_001)


def test_month_end_discount_run_slow(lines: tuple[InvoiceLine, ...]) -> None:
    """Deliberately slow (0.8 s) so the sharding demo has uneven test durations."""
    time.sleep(0.8)
    invoices = [Invoice(customer=f"Customer {n}", region="GB", lines=lines) for n in range(1, 4)]
    discounted = [apply_discount(invoice, 100 * n) for n, invoice in enumerate(invoices, start=1)]
    assert [invoice.subtotal_cents for invoice in discounted] == [3_900, 3_800, 3_700]
