export enum Currency {
  USD = "USD",
  EUR = "EUR",
  GBP = "GBP",
}

const SYMBOLS: Record<Currency, string> = { USD: "$", EUR: "€", GBP: "£" };

/** Takes `percent` percent off a total in cents, rounding half a cent up. */
export function applyDiscount(totalCents: number, percent: number): number {
  if (percent <= 0) {
    return totalCents;
  }
  const off = Math.round((totalCents * percent) / 100);
  return totalCents - off;
}

/** Formats cents as a price: "$12.50", or "12.50 €" for euros. */
export function formatPrice(cents: number, currency: Currency): string {
  const units = (cents / 100).toFixed(2);
  return currency === Currency.EUR ? `${units} ${SYMBOLS[currency]}` : `${SYMBOLS[currency]}${units}`;
}

export class Cart {
  private readonly lines = new Map<string, number>();

  addItem(sku: string, qty: number): void {
    if (qty <= 0) {
      throw new RangeError("qty must be positive");
    }
    this.lines.set(sku, (this.lines.get(sku) ?? 0) + qty);
  }

  count(): number {
    let n = 0;
    for (const qty of this.lines.values()) n += qty;
    return n;
  }
}
