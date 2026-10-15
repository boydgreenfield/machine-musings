import { describe, expect, it } from "vitest";
import { rateFor } from "../src/shipping";

describe("rateFor", () => {
  it("charges the base rate up to a kilogram", () => {
    expect(rateFor("domestic", [200, 300])).toBe(300);
  });

  it("charges extra weight above a kilogram", () => {
    expect(rateFor("eu", [800, 700, 600])).toBe(1100);
  });

  it.skip("ships worldwide", () => {
    expect(rateFor("world", [100])).toBe(1500);
  });
});
