/**
 * The `setupFiles` entry (`coversetSetup`): runs in every test worker before each test file.
 *
 * `beforeEach` closes the previous checkpoint of Istanbul's counters (whatever ran outside a test,
 * module loading included, goes to `<import>`) and tells the recorder which test is running;
 * `afterEach` writes the lines and arcs the test executed; `afterAll` writes what ran after the
 * last test and the recorder's observations.
 *
 * Concurrent tests (`test.concurrent`, `describe.concurrent`, `sequence.concurrent`) overlap in
 * one worker, and Istanbul's counters are shared, so a counter delta cannot be split between
 * them. While tests overlap, each delta is written to every active test: a test's lines are then
 * what ran while it was active (a superset of its own, never fewer), instead of losing them to
 * whichever test started last. Each overlap is spooled (`{k: "overlap", tasks}`) and the reporter
 * warns and counts the tests. Shapes stay exact (the recorder's async-local test id). Rows are appended to a per-worker JSONL spool under
 * `COVERSET_SPOOL` (set by the reporter to its run's directory; `.coverset/spool` under the
 * working directory otherwise; `off` when the reporter writes nothing).
 * Being registered first, these hooks run before the test file's own `beforeEach` and, with
 * Vitest's default `sequence.hooks: "stack"`, after its `afterEach`, so a test's hooks count as
 * part of the test.
 *
 * The counter checkpoint and the recorder live on `globalThis`, once per worker: with
 * `isolate: false` this file runs again for every test file in the same worker, and a fresh
 * checkpoint would hand everything earlier test files executed to `<import>`.
 */
import { randomUUID } from "node:crypto";
import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { threadId } from "node:worker_threads";

import { afterAll, afterEach, beforeAll, beforeEach } from "vitest";

import { CounterTracker, type FileDelta } from "./counters.js";
import { install } from "./recorder.js";
import { SPOOL_ENV, SPOOL_OFF } from "./spool.js";

declare global {
  // eslint-disable-next-line no-var
  var __coverset_tracker__: CounterTracker | undefined;
  // eslint-disable-next-line no-var
  var __coverset_spool_failed__: boolean | undefined;
  // eslint-disable-next-line no-var
  var __coverset_active__: string[] | undefined;
}

const recorder = install();
const tracker = (globalThis.__coverset_tracker__ ??= new CounterTracker());
const spoolDir = process.env[SPOOL_ENV] || join(process.cwd(), ".coverset", "spool");
const spoolFile = join(spoolDir, `${process.pid}-${threadId}-${randomUUID().slice(0, 8)}.jsonl`);
let ready = false;

/** The reporter could not set up a spool and writes nothing: neither does this worker. */
const off = process.env[SPOOL_ENV] === SPOOL_OFF;

function write(row: object): void {
  if (off || globalThis.__coverset_spool_failed__) return;
  try {
    if (!ready) {
      mkdirSync(spoolDir, { recursive: true });
      ready = true;
    }
    appendFileSync(spoolFile, JSON.stringify(row) + "\n");
  } catch (error) {
    // Once per worker: the reporter says what is missing from the bundle.
    globalThis.__coverset_spool_failed__ = true;
    console.warn(`coverset-vitest: cannot write the spool ${spoolFile}, this worker records nothing more: ${String(error)}`);
  }
}

function writeCoverage(task: string | null, delta: Map<string, FileDelta>): void {
  if (delta.size && !off) write({ k: "cov", task, files: Object.fromEntries(delta) });
}

beforeAll(() => {
  writeCoverage(null, tracker.delta());
});

/** Tests between their `beforeEach` and `afterEach` in this worker (more than one only when concurrent). */
const active: string[] = (globalThis.__coverset_active__ ??= []);

function writeToActive(delta: Map<string, FileDelta>): void {
  if (active.length === 0) writeCoverage(null, delta);
  else for (const task of active) writeCoverage(task, delta);
}

beforeEach((context) => {
  const concurrent = context.task.concurrent === true;
  // Vitest never runs a sequential test alongside others: anything still listed is stale (a test
  // whose afterEach never ran) and must not collect this test's lines.
  if (!concurrent) active.length = 0;
  writeToActive(tracker.delta());
  const id = context.task.id;
  if (active.length > 0 && !off) write({ k: "overlap", tasks: [...active, id] });
  if (!active.includes(id)) active.push(id);
  recorder.beginTest(id, concurrent);
});

afterEach((context) => {
  writeToActive(tracker.delta());
  const id = context.task.id;
  const at = active.indexOf(id);
  if (at >= 0) active.splice(at, 1);
  recorder.endTest(id);
});

afterAll(() => {
  active.length = 0;
  writeCoverage(null, tracker.delta());
  write({ k: "shapes", ...recorder.dump() });
});
