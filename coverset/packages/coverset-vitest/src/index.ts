/**
 * coverset-vitest: a Vitest reporter that writes coverset's neutral bundle (per-test coverage
 * from Istanbul's counters, function regions with declared types, argument shapes).
 *
 * ```ts
 * // vitest.config.ts
 * plugins: [coversetPlugin({ include: ["src"] })],
 * test: {
 *   setupFiles: [coversetSetup],
 *   reporters: ["default", coversetReporter()],
 *   coverage: { enabled: true, provider: "istanbul", include: ["src/**"] },
 * }
 * ```
 */
export { coversetPlugin, coversetSetup, type CoversetPluginOptions } from "./plugin.js";
export { coversetReporter, CoversetReporter, type CoversetReporterOptions } from "./reporter.js";
export { shapeKey, stringKind, lenBucket } from "./features.js";
export { exampleRepr, scrub } from "./redact.js";
export { PRODUCER, VERSION } from "./version.js";

declare module "vitest" {
  /** Test metadata coverset-vitest reads (`test("...", { meta: { invariant: "cents_conserved" } }, fn)`). */
  interface TaskMeta {
    /** Invariant id(s) the test checks: `invariants` in `tests.jsonl` and rows of `links.jsonl`. */
    invariant?: string | string[];
    invariants?: string[];
    /** Marks a property-based test (detected automatically for fast-check's `fc.assert`). */
    property?: boolean;
  }
}
