/**
 * The `setupFiles` entry (`coversetSetup`): runs in every test worker before each test file.
 *
 * `beforeEach` closes the previous checkpoint of Istanbul's counters (whatever ran outside a test,
 * module loading included, goes to `<import>`) and tells the recorder which test is running;
 * `afterEach` writes the lines and arcs the test executed; `afterAll` writes what ran after the
 * last test and the recorder's observations. Rows are appended to a per-worker JSONL spool under
 * `COVERSET_SPOOL` (set by the reporter; `.coverset/spool` under the working directory otherwise).
 * Being registered first, these hooks run before the test file's own `beforeEach` and, with
 * Vitest's default `sequence.hooks: "stack"`, after its `afterEach`, so a test's hooks count as
 * part of the test.
 */
import { randomUUID } from "node:crypto";
import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { threadId } from "node:worker_threads";

import { afterAll, afterEach, beforeAll, beforeEach } from "vitest";

import { CounterTracker, type FileDelta } from "./counters.js";
import { install } from "./recorder.js";
import { SPOOL_ENV } from "./spool.js";

const recorder = install();
const tracker = new CounterTracker();
const spoolDir = process.env[SPOOL_ENV] || join(process.cwd(), ".coverset", "spool");
const spoolFile = join(spoolDir, `${process.pid}-${threadId}-${randomUUID().slice(0, 8)}.jsonl`);
let ready = false;

function write(row: object): void {
  try {
    if (!ready) {
      mkdirSync(spoolDir, { recursive: true });
      ready = true;
    }
    appendFileSync(spoolFile, JSON.stringify(row) + "\n");
  } catch (error) {
    console.warn(`coverset-vitest: cannot write the spool ${spoolFile}: ${String(error)}`);
  }
}

function writeCoverage(task: string | null, delta: Map<string, FileDelta>): void {
  if (delta.size) write({ k: "cov", task, files: Object.fromEntries(delta) });
}

beforeAll(() => {
  writeCoverage(null, tracker.delta());
});

beforeEach((context) => {
  writeCoverage(null, tracker.delta());
  recorder.setTest(context.task.id);
});

afterEach((context) => {
  writeCoverage(context.task.id, tracker.delta());
  recorder.setTest(null);
});

afterAll(() => {
  writeCoverage(null, tracker.delta());
  write({ k: "shapes", ...recorder.dump() });
});
