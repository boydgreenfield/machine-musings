/** `tests.jsonl` rows from Vitest's test cases, and the shared branch-target rule. */
import { describe, expect, it } from "vitest";
import type { TestCase } from "vitest/node";

import { branchTargets } from "../src/branches.js";
import { testRowOf, toJson, uniqueIds } from "../src/reporter.js";

function fakeCase(over: Record<string, unknown>): TestCase {
  return {
    id: "abc_0",
    name: "t",
    fullName: "suite > t",
    location: { line: 3, column: 3 },
    tags: [],
    options: { mode: "run" },
    meta: () => ({}),
    annotations: () => [],
    diagnostic: () => ({ duration: 12.5, startTime: 1_760_000_000_000 }),
    result: () => ({ state: "passed", errors: undefined }),
    ...over,
  } as unknown as TestCase;
}

const rel = (abs: string) => (abs.startsWith("/repo/") && !abs.includes("node_modules") ? abs.slice(6) : null);
const SOURCE = ['import fc from "fast-check";', "", 'it("plain", () => {});', 'it("prop", () => {', "  fc.assert(fc.property(fc.nat(), () => true));", "});"];

describe("testRowOf", () => {
  it("maps a passing test", () => {
    const row = testRowOf(fakeCase({}), "f.test.ts > suite > t", "f.test.ts", SOURCE, [3, 4], rel);
    expect(row).toMatchObject({ outcome: "passed", duration_s: 0.0125, line: 3, property: false, start: 1_760_000_000, stop: 1_760_000_000.0125 });
  });

  it("signs a failure by its error type and first in-repo frame, never by its message", () => {
    const failure = (message: string, line: number) =>
      fakeCase({
        result: () => ({
          state: "failed",
          errors: [{ name: "AssertionError", message, stacks: [{ file: "/repo/node_modules/x.js", line: 1 }, { file: "/repo/tests/f.test.ts", line }] }],
        }),
      });
    const a = testRowOf(failure("expected 1 to be 2 ghp_" + "a".repeat(30), 7), "id", "f.test.ts", null, [], rel);
    const b = testRowOf(failure("expected 3 to be 4", 7), "id", "f.test.ts", null, [], rel);
    const c = testRowOf(failure("expected 1 to be 2", 8), "id", "f.test.ts", null, [], rel);
    expect(a["outcome"]).toBe("failed");
    expect(a["failed_phase"]).toBe("call");
    expect(a["message"]).toBe("AssertionError: expected 1 to be 2 ghp_[REDACTED]");
    expect(a["fail_sig"]).toMatch(/^[0-9a-f]{16}$/);
    expect(a["fail_sig"]).toBe(b["fail_sig"]);
    expect(a["fail_sig"]).not.toBe(c["fail_sig"]);
  });

  it("maps skip, todo, fails, tags, meta and fast-check", () => {
    const skipped = testRowOf(fakeCase({ options: { mode: "todo" }, result: () => ({ state: "skipped" }), diagnostic: () => undefined }), "id", "f", null, [], rel);
    expect(skipped).toMatchObject({ outcome: "skipped", markers: ["todo"], duration_s: null, start: null });
    const pending = testRowOf(fakeCase({ result: () => ({ state: "pending" }) }), "id", "f", null, [], rel);
    expect(pending["outcome"]).toBeNull();
    const xfail = testRowOf(fakeCase({ options: { mode: "run", fails: true } }), "id", "f", null, [], rel);
    expect(xfail).toMatchObject({ outcome: "xfailed", markers: ["xfail"] });
    const tagged = testRowOf(
      fakeCase({ tags: ["slow"], meta: () => ({ invariant: "cents_conserved", property: true }) }),
      "id",
      "f",
      null,
      [],
      rel,
    );
    expect(tagged).toMatchObject({ markers: ["slow"], invariants: ["cents_conserved"], property: true });
    const prop = testRowOf(fakeCase({ location: { line: 4, column: 1 } }), "id", "f", SOURCE, [3, 4], rel);
    expect(prop["property"]).toBe(true);
  });
});

describe("branchTargets", () => {
  const statements = [3, 4, 5, 7, 9, 10, 12];
  it("sends an if to its consequent and a missing else past the if", () => {
    expect(branchTargets("if", 4, 6, [{ start: 4, end: 6 }, { start: null, end: null }], statements)).toEqual([5, 7]);
  });
  it("sends an else to its first statement", () => {
    expect(branchTargets("if", 4, 12, [{ start: 4, end: 12 }, { start: 8, end: 12 }], statements)).toEqual([5, 9]);
  });
  it("exits when nothing follows, and keeps one-line expressions on their line", () => {
    expect(branchTargets("if", 12, 13, [{ start: 12, end: 13 }, { start: null, end: null }], statements)).toEqual([12, -12]);
    expect(branchTargets("binary-expr", 9, 9, [{ start: 9, end: 9 }, { start: 9, end: 9 }], statements)).toEqual([9, 9]);
  });
});

describe("uniqueIds", () => {
  it("numbers repeated ids instead of dropping the tests", () => {
    // test.each([[1], [1]])("dup %s") gives two tests with one title.
    expect(uniqueIds(["f.test.ts > dup 1", "f.test.ts > dup 1", "f.test.ts > dup 1", "f.test.ts > other"])).toEqual([
      "f.test.ts > dup 1",
      "f.test.ts > dup 1 [2]",
      "f.test.ts > dup 1 [3]",
      "f.test.ts > other",
    ]);
  });

  it("cuts long ids to 512 characters with a hash, unique and well-formed", () => {
    const long = "f.test.ts > " + "x".repeat(600);
    const other = "f.test.ts > " + "x".repeat(599) + "y";
    const ids = uniqueIds([long, other, long, "f.test.ts > " + "\u{1F600}".repeat(300)]);
    for (const id of ids) {
      expect(id.length).toBeLessThanOrEqual(512);
      expect(id.isWellFormed()).toBe(true);
    }
    expect(new Set(ids).size).toBe(4);
    expect(ids[0]).toMatch(/^f\.test\.ts > x+ #[0-9a-f]{16}$/);
    expect(ids[2]).toMatch(/ #[0-9a-f]{16} \[2\]$/);
  });

  it("makes lone surrogates U+FFFD before comparing", () => {
    expect(uniqueIds(["f > a\ud800", "f > a\ufffd"])).toEqual(["f > a\ufffd", "f > a\ufffd [2]"]);
  });
});

describe("toJson", () => {
  it("never writes a lone surrogate, raw or escaped", () => {
    const text = toJson({ id: "t \ud800 x", nested: ["\udc00"], ok: "\u{1F600}" });
    expect(text).not.toMatch(/\\ud[89a-f]/i);
    expect(JSON.parse(text)).toEqual({ id: "t \ufffd x", nested: ["\ufffd"], ok: "\u{1F600}" });
  });
});
