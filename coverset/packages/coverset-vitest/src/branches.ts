/**
 * Branch arcs from Istanbul's branch map, as `[branch line, target line]` pairs (`files.jsonl`'s
 * `branches`, `arcs.jsonl`, per-test `arcs`), shared by the worker and the reporter so both
 * derive the same pairs.
 *
 * Istanbul gives an `if` two locations: the whole `if` statement (taken when the condition holds)
 * and the `else` part, which is empty when there is no `else`. The target of the first is the
 * first statement after the `if` line inside the consequent, of an `else` its first statement, and
 * of a missing `else` the first statement after the whole `if` (where control goes when the
 * condition is false), or the negated branch line when nothing follows (an exit, as coverage.py
 * writes one). For every other kind (`cond-expr`, `binary-expr`, `switch`, `default-arg`, ...) the
 * target is the first statement inside the location, else the location's first line. A target on
 * the branch line itself (`a ?? b` on one line) is a valid arc.
 */
export interface BranchLocation {
  /** First and last line of the location; null for an empty one. */
  start: number | null;
  end: number | null;
}

function firstAtLeast(sorted: number[], min: number, max = Infinity): number | undefined {
  for (const n of sorted) {
    if (n > max) return undefined;
    if (n >= min) return n;
  }
  return undefined;
}

/** The target line of every location of one branch (`statements` sorted ascending). */
export function branchTargets(type: string, from: number, end: number, locations: BranchLocation[], statements: number[]): number[] {
  const after = (): number => firstAtLeast(statements, Math.max(end, from) + 1) ?? -from;
  if (type === "if") {
    const alternate = locations[1];
    const hasElse = alternate !== undefined && alternate.start !== null;
    return locations.map((loc, j) => {
      if (j === 0) {
        const upper = hasElse ? alternate.start! - 1 : end;
        return firstAtLeast(statements, from + 1, upper) ?? from;
      }
      if (loc.start === null) return after();
      return firstAtLeast(statements, Math.max(loc.start, from + 1), loc.end ?? loc.start) ?? loc.start;
    });
  }
  return locations.map((loc) => {
    if (loc.start === null) return after();
    return firstAtLeast(statements, loc.start, loc.end ?? loc.start) ?? loc.start;
  });
}
