import { describe, expect, it } from "vitest";
import { mergeRanges } from "../src/ranges";

describe("mergeRanges", () => {
  it("merges overlapping ranges", () => {
    expect(mergeRanges([[1, 3], [2, 6], [8, 10]])).toEqual([[1, 6], [8, 10]]);
  });

  it("keeps disjoint ranges apart", () => {
    expect(mergeRanges([[1, 2], [5, 6]])).toEqual([[1, 2], [5, 6]]);
  });
});
