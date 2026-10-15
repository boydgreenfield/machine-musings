/**
 * Concurrent tests (`test.concurrent`): the recorder attributes each call to the test whose async
 * continuation made it, and the reporter warns about the shared Istanbul counters.
 */
import { describe, expect, it } from "vitest";
import type { TestCase } from "vitest/node";

import { Recorder } from "../src/recorder.js";
import { concurrencyWarning, testRowOf } from "../src/reporter.js";

const SPEC = '{"p":["x"]}';
const tick = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** What Vitest does for one test: an async chain that runs `beforeEach`, the body, `afterEach`. */
async function runTest(recorder: Recorder, id: string, concurrent: boolean, body: () => Promise<void>): Promise<void> {
  await Promise.resolve();
  recorder.beginTest(id, concurrent);
  await body();
  recorder.endTest(id);
}

function call(recorder: Recorder, value: unknown): void {
  recorder.leave(recorder.enter("src/f.ts", 0, "f", 1, SPEC, 0, [value], undefined), value);
}

function testsByShape(recorder: Recorder): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const o of recorder.dump().obs.filter((o) => o.p === "x")) out[o.s] = [...o.t].sort();
  return out;
}

describe("the recorder with concurrent tests", () => {
  it("gives each call to the test whose continuation made it", async () => {
    const recorder = new Recorder();
    await Promise.all([
      runTest(recorder, "a", true, async () => {
        await tick(5);
        call(recorder, 1); // after b began: a single current test would say b
        await tick(10);
        call(recorder, 2);
      }),
      runTest(recorder, "b", true, async () => {
        await tick(1);
        call(recorder, "s");
        await tick(20);
        call(recorder, "t"); // after a ended
      }),
    ]);
    expect(testsByShape(recorder)).toEqual({ "int|pos": ["a"], "str|ascii|len:1": ["b"] });
    expect(recorder.activeTests).toBe(0);
    expect(recorder.currentTest).toBeNull();
    expect(recorder.errors).toBe(0);
  });

  it("needs no async storage for sequential tests, and drops a stale test", async () => {
    const recorder = new Recorder();
    await runTest(recorder, "a", false, async () => call(recorder, 1));
    recorder.beginTest("lost", false); // its afterEach never ran
    recorder.beginTest("b", false);
    call(recorder, "s");
    recorder.endTest("b");
    expect(testsByShape(recorder)).toEqual({ "int|pos": ["a"], "str|ascii|len:1": ["b"] });
    expect(recorder.activeTests).toBe(0);
  });

  it("keeps setTest for callers that drive one test at a time", () => {
    const recorder = new Recorder();
    recorder.setTest("t");
    call(recorder, 1);
    recorder.setTest(null);
    call(recorder, 2); // outside a test: counted, not written
    expect(testsByShape(recorder)).toEqual({ "int|pos": ["t"] });
  });
});

describe("the concurrency warning", () => {
  it("is silent when no tests overlapped (a sequential run, or --maxConcurrency=1)", () => {
    expect(concurrencyWarning(0, 0)).toBeNull();
    expect(concurrencyWarning(4, 0)).toBeNull();
  });

  it("says what is attributed how and what to do", () => {
    const text = concurrencyWarning(3, 2)!;
    expect(text).toMatch(/^2 test\(s\) ran while another test of the same worker was running \(3 marked/);
    expect(text).toContain("superset of its own");
    expect(text).toContain("--maxConcurrency=1");
  });

  it("marks a concurrent test in tests.jsonl", () => {
    const testCase = {
      id: "abc_0",
      name: "t",
      fullName: "suite > t",
      location: { line: 3, column: 3 },
      tags: [],
      options: { mode: "run", concurrent: true },
      meta: () => ({}),
      annotations: () => [],
      diagnostic: () => ({ duration: 1, startTime: 1_760_000_000_000 }),
      result: () => ({ state: "passed", errors: undefined }),
    } as unknown as TestCase;
    const row = testRowOf(testCase, "f > suite > t", "f.test.ts", null, [], () => null);
    expect(row["markers"]).toEqual(["concurrent"]);
  });
});
