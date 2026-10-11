export enum Currency {
  USD = "USD",
  EUR = "EUR",
}

export function applyDiscount(totalCents: number, percent: number): number {
  if (percent <= 0) {
    return totalCents;
  }
  const off = Math.round((totalCents * percent) / 100);
  return totalCents - off;
}

export function formatPrice(cents: number, currency: Currency): string {
  const units = (cents / 100).toFixed(2);
  return currency === Currency.EUR ? `${units} €` : `$${units}`;
}

export class Cart {
  private items = new Map<string, number>();

  addItem(sku: string, qty: number): void {
    if (qty <= 0) {
      throw new RangeError("qty must be positive");
    }
    this.items.set(sku, (this.items.get(sku) ?? 0) + qty);
  }
}
