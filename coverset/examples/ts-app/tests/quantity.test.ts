import { describe, expect, it } from "vitest";
import { parseQuantity } from "../src/quantity";

describe("parseQuantity", () => {
  it("reads digits", () => {
    expect(parseQuantity("3")).toBe(3);
  });

  it("returns undefined for blank input", () => {
    expect(parseQuantity("   ")).toBeUndefined();
  });

  it("returns undefined for words", () => {
    expect(parseQuantity("some")).toBeUndefined();
  });
});
