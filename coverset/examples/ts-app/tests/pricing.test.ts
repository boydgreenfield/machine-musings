import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { Cart, Currency, applyDiscount, formatPrice } from "../src/pricing";

describe("applyDiscount", () => {
  it("takes 10% off", { meta: { invariant: "discount_never_negative" } }, () => {
    expect(applyDiscount(1000, 10)).toBe(900);
  });

  it("rounds half a cent up", () => {
    expect(applyDiscount(1999, 2.5)).toBe(1949);
  });

  it("leaves the total alone without a discount", () => {
    expect(applyDiscount(1000, 0)).toBe(1000);
  });
});

describe("formatPrice", () => {
  it("formats dollars", () => {
    expect(formatPrice(1250, Currency.USD)).toBe("$12.50");
  });

  it("puts the euro sign last", () => {
    expect(formatPrice(1999, Currency.EUR)).toBe("19.99 €");
  });

  it("always shows two decimals", () => {
    fc.assert(
      fc.property(fc.integer({ min: 0, max: 10_000_000 }), (cents) => /\.\d\d$/.test(formatPrice(cents, Currency.USD))),
      { seed: 42 },
    );
  });

  it.todo("formats currencies without minor units");
});

describe("Cart", () => {
  it("adds items", () => {
    const cart = new Cart();
    cart.addItem("sku-1", 2);
    cart.addItem("sku-1", 1);
    expect(cart.count()).toBe(3);
  });

  it("rejects a zero quantity", () => {
    expect(() => new Cart().addItem("sku-1", 0)).toThrow(RangeError);
  });
});
