/**
 * Per-test lines and branch arcs from Istanbul's counters (worker side).
 *
 * Vitest's Istanbul provider keeps one coverage object per instrumented file in
 * `globalThis.__VITEST_COVERAGE__`: statement and branch locations in the transformed code, hit
 * counters, and the source map back to the original file. {@link CounterTracker.delta} compares
 * the counters with the previous checkpoint and returns, per file, the original first line of
 * every statement whose counter moved and the `[branch line, target first line]` arc of every
 * branch location whose counter moved. Vitest's own counters are only read, never reset.
 *
 * Locations are mapped to the original source as `istanbul-lib-source-maps` maps them (greatest
 * lower bound, then least upper bound); a statement that does not map is skipped. Branch
 * targets follow `branches.ts`.
 */
import { branchTargets } from "./branches.js";
import { GREATEST_LOWER_BOUND, LEAST_UPPER_BOUND, originalPositionFor, TraceMap } from "@jridgewell/trace-mapping";

export const COVERAGE_KEY = "__VITEST_COVERAGE__";

interface Position {
  line?: number;
  column?: number;
}

interface Location {
  start: Position;
  end: Position;
}

interface FileCoverage {
  path: string;
  statementMap: Record<string, Location>;
  branchMap: Record<string, { loc: Location; type: string; locations: Location[]; line?: number }>;
  s: Record<string, number>;
  b: Record<string, number[]>;
  inputSourceMap?: ConstructorParameters<typeof TraceMap>[0];
}

interface Tracked {
  data: FileCoverage;
  /** Original first line per statement id (0 when it does not map). */
  lines: Map<string, number>;
  /** Per branch id: its original line and the target line of each location (0 when unmapped). */
  arcs: Map<string, { from: number; targets: number[] }>;
  prevS: Map<string, number>;
  prevB: Map<string, number[]>;
}

export interface FileDelta {
  /** Original lines of statements executed since the last checkpoint (sorted). */
  l: number[];
  /** Branch arcs executed since the last checkpoint. */
  a: [number, number][];
}

function mapper(data: FileCoverage): (pos: Position) => number {
  if (data.inputSourceMap === undefined) return (pos) => pos.line ?? 0;
  let map: TraceMap;
  try {
    map = new TraceMap(data.inputSourceMap);
  } catch {
    return () => 0;
  }
  return (pos) => {
    if (pos.line === undefined || pos.column === undefined || pos.line === null || pos.column === null) return 0;
    const glb = originalPositionFor(map, { line: pos.line, column: pos.column, bias: GREATEST_LOWER_BOUND });
    if (glb.line !== null) return glb.line;
    const lub = originalPositionFor(map, { line: pos.line, column: pos.column, bias: LEAST_UPPER_BOUND });
    return lub.line ?? 0;
  };
}

function isEmpty(loc: Location): boolean {
  return loc.start.line === undefined || loc.start.line === null;
}

function track(data: FileCoverage): Tracked {
  const toLine = mapper(data);
  const lines = new Map<string, number>();
  for (const [id, loc] of Object.entries(data.statementMap)) lines.set(id, toLine(loc.start));
  const sorted = [...new Set(lines.values())].filter((n) => n > 0).sort((a, b) => a - b);
  const arcs = new Map<string, { from: number; targets: number[] }>();
  for (const [id, branch] of Object.entries(data.branchMap)) {
    const from = toLine(branch.loc.start);
    const end = toLine(branch.loc.end);
    const locations = branch.locations.map((loc) =>
      isEmpty(loc) ? { start: null, end: null } : { start: toLine(loc.start), end: toLine(loc.end) },
    );
    arcs.set(id, { from, targets: from > 0 ? branchTargets(branch.type, from, Math.max(from, end), locations, sorted) : [] });
  }
  return { data, lines, arcs, prevS: new Map(), prevB: new Map() };
}

export class CounterTracker {
  private readonly files = new Map<string, Tracked>();

  /** Counters that moved since the last call, per original file path; then a new checkpoint. */
  delta(): Map<string, FileDelta> {
    const out = new Map<string, FileDelta>();
    const store = (globalThis as Record<string, unknown>)[COVERAGE_KEY] as Record<string, FileCoverage> | undefined;
    if (store === undefined || store === null) return out;
    for (const [key, data] of Object.entries(store)) {
      if (data === null || typeof data !== "object" || data.s === undefined) continue;
      let tracked = this.files.get(key);
      if (tracked === undefined || tracked.data !== data) {
        tracked = track(data);
        this.files.set(key, tracked);
      }
      const lines = new Set<number>();
      const arcs = new Map<string, [number, number]>();
      // A counter below its checkpoint means Vitest reset the counters (a new test file in a
      // reused worker): count from zero again.
      let reset = false;
      for (const [id, count] of Object.entries(data.s)) {
        if (count < (tracked.prevS.get(id) ?? 0)) reset = true;
      }
      for (const [id, count] of Object.entries(data.s)) {
        const prev = reset ? 0 : (tracked.prevS.get(id) ?? 0);
        if (count > prev) {
          const line = tracked.lines.get(id) ?? 0;
          if (line > 0) lines.add(line);
        }
        tracked.prevS.set(id, count);
      }
      for (const [id, counts] of Object.entries(data.b)) {
        const prev = reset ? undefined : tracked.prevB.get(id);
        const info = tracked.arcs.get(id);
        counts.forEach((count, j) => {
          if (count > (prev?.[j] ?? 0) && info !== undefined) {
            const target = info.targets[j] ?? 0;
            if (info.from > 0 && target !== 0) arcs.set(`${info.from},${target}`, [info.from, target]);
          }
        });
        tracked.prevB.set(id, [...counts]);
      }
      if (lines.size || arcs.size) {
        out.set(data.path ?? key, {
          l: [...lines].sort((a, b) => a - b),
          a: [...arcs.values()].sort((x, y) => x[0] - y[0] || x[1] - y[1]),
        });
      }
    }
    return out;
  }
}
