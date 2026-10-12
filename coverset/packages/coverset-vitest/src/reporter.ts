/**
 * `coversetReporter()`: merges the workers' spools with Vitest's results and Istanbul's coverage
 * map and writes the neutral bundle (`docs/neutral-bundle.md`) to `.coverset/neutral/`.
 *
 * Vitest calls `onCoverage` (the remapped coverage map of every measured file, untested ones
 * included) before `onTestRunEnd`; the bundle is written in `onTestRunEnd`.
 *
 * Workers spool into a directory of this run's own (`run-<pid>-<8 hex>` under `COVERSET_SPOOL`,
 * default `.coverset/spool`), removed once the bundle is written, so concurrent runs over one
 * project never read each other's rows. When that directory cannot be created (`.coverset` is a
 * file, a read-only tree) the reporter warns and writes nothing; the test run is unaffected.
 */
import { createHash, randomBytes } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, renameSync, rmdirSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import type { Reporter, TestCase, TestModule, Vitest } from "vitest/node";

import { branchTargets } from "./branches.js";
import { relPath, repoRoot } from "./paths.js";
import { redactMessage } from "./redact.js";
import type { ShapeDump } from "./recorder.js";
import { SPOOL_ENV, SPOOL_OFF } from "./spool.js";
import { PRODUCER, VERSION } from "./version.js";

export const OUT_ENV = "COVERSET_OUT";
export const SHARD_ENV = "COVERSET_SHARD";
export const IMPORT_ID = "<import>";
const MAX_ID = 512;
/** `<pid>-<thread id>-<8 hex>.jsonl`, the name `setup.ts` gives a worker's spool. */
const SPOOL_FILE = /^\d+-\d+-[0-9a-f]{8}\.jsonl$/;
/** `run-<pid>-<8 hex>`, a run's spool directory. */
const RUN_DIR = /^run-(\d+)-[0-9a-f]{8}$/;
const MAX_EXAMPLE = 200;
const BUNDLE_FILES = ["manifest.json", ...["tests", "files", "coverage", "arcs", "functions", "shapes", "links"].flatMap((s) => [`${s}.jsonl`, `${s}.jsonl.gz`])];

export interface CoversetReporterOptions {
  /** Directory bundle paths are relative to (default: `COVERSET_ROOT`, else the git work tree). */
  root?: string;
  /** Output directory (default: `COVERSET_OUT`, else `.coverset/neutral` under Vitest's root). */
  outDir?: string;
  /** Write `functions.jsonl` types through the TypeScript checker (default true). */
  types?: boolean;
}

interface Location {
  start: { line?: number | null; column?: number | null };
  end: { line?: number | null; column?: number | null };
}

interface FileCoverageData {
  path: string;
  statementMap: Record<string, Location>;
  branchMap: Record<string, { loc: Location; type: string; locations: Location[]; line?: number }>;
  s: Record<string, number>;
  b: Record<string, number[]>;
}

interface SpoolCov {
  k: "cov";
  task: string | null;
  files: Record<string, { l: number[]; a: [number, number][] }>;
}

type SpoolShapes = { k: "shapes" } & ShapeDump;

interface StaticFile {
  path: string;
  abs: string;
  nLines: number;
  statements: Set<number>;
  branches: Map<string, [number, number]>;
  executedArcs: Map<string, [number, number]>;
}

function sha16(text: string): string {
  return createHash("sha256").update(text).digest("hex").slice(0, 16);
}

/** Lines as Python counts them (`\n`, `\r\n` and a lone `\r` each end one). */
function lineCount(text: string): number {
  if (!text.length) return 0;
  const parts = text.split(/\r\n|\r|\n/);
  return parts[parts.length - 1] === "" ? parts.length - 1 : parts.length;
}

function arcKey(a: number, b: number): string {
  return `${a},${b}`;
}

/** The static facts of one file of the remapped coverage map: statements, possible and executed arcs. */
function staticFile(data: FileCoverageData, path: string): StaticFile {
  let text = "";
  try {
    text = readFileSync(data.path, "utf8");
  } catch {
    // a vanished file keeps the largest line it mentions
  }
  const statements = new Set<number>();
  for (const loc of Object.values(data.statementMap)) {
    if (loc.start.line) statements.add(loc.start.line);
  }
  const sorted = [...statements].sort((a, b) => a - b);
  const branches = new Map<string, [number, number]>();
  const executed = new Map<string, [number, number]>();
  for (const [id, branch] of Object.entries(data.branchMap)) {
    const from = branch.loc.start.line ?? branch.line;
    if (!from) continue;
    const end = Math.max(from, branch.loc.end.line ?? from);
    const locations = branch.locations.map((loc) =>
      loc.start.line ? { start: loc.start.line, end: loc.end.line ?? loc.start.line } : { start: null, end: null },
    );
    const targets = branchTargets(branch.type, from, end, locations, sorted);
    targets.forEach((target, j) => {
      const key = arcKey(from, target);
      branches.set(key, [from, target]);
      if ((data.b[id]?.[j] ?? 0) > 0) executed.set(key, [from, target]);
    });
  }
  const maxLine = Math.max(0, ...statements, ...[...branches.values()].flat().map(Math.abs));
  return {
    path,
    abs: data.path,
    nLines: text ? lineCount(text) : maxLine,
    statements,
    branches,
    executedArcs: executed,
  };
}

function sortArcs(arcs: Iterable<[number, number]>): [number, number][] {
  return [...arcs].sort((x, y) => x[0] - y[0] || x[1] - y[1]);
}

/** `JSON.stringify` replacer: every string well-formed (a lone surrogate becomes U+FFFD, never `\\udXXX`). */
function wellFormedStrings(_key: string, value: unknown): unknown {
  return typeof value === "string" ? value.toWellFormed() : value;
}

/** `JSON.stringify` with every string value well-formed (row keys are the reporter's own). */
export function toJson(value: unknown, space?: number): string {
  return JSON.stringify(value, wellFormedStrings, space);
}

function jsonl(rows: object[]): string {
  return rows.map((r) => toJson(r)).join("\n") + (rows.length ? "\n" : "");
}

/**
 * Bundle test ids for Vitest's `<file> > <full name>` ids, in order: unique and at most
 * {@link MAX_ID} characters. A repeated id gets ` [2]`, ` [3]`, ... (as `test.each` rows with the
 * same title do); one that is too long keeps its head and ends with ` #` and 16 hex digits of the
 * sha256 of the whole id. Lone surrogates become U+FFFD first, so ids are what the bundle stores.
 */
export function uniqueIds(raw: readonly string[]): string[] {
  const seen = new Set<string>();
  const fit = (id: string, suffix: string): string => {
    if (id.length + suffix.length <= MAX_ID) return id + suffix;
    const tag = ` #${sha16(id)}`;
    let head = id.slice(0, MAX_ID - suffix.length - tag.length);
    if (/[\ud800-\udbff]$/.test(head)) head = head.slice(0, -1);
    return head + tag + suffix;
  };
  return raw.map((original) => {
    const base = original.toWellFormed();
    let id = fit(base, "");
    for (let n = 2; seen.has(id); n++) id = fit(base, ` [${n}]`);
    seen.add(id);
    return id;
  });
}

function stringList(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.filter((v): v is string => typeof v === "string");
  return [];
}

const PROPERTY_CALLS = /\bfc\s*\.\s*(assert|property|asyncProperty)\s*\(|\btest\s*\.\s*prop\s*\(|\bit\s*\.\s*prop\s*\(/;

function round6(x: number): number {
  return Math.round(x * 1e6) / 1e6;
}

/**
 * The `tests.jsonl` row of one Vitest test case. `source` is the test file's lines (for the
 * fast-check heuristic), `lines` the first lines of every test in the file, `rel` maps an
 * absolute path to a repository path (null outside it) for the failure signature's frame.
 */
export function testRowOf(
  testCase: TestCase,
  id: string,
  file: string,
  source: string[] | null,
  lines: number[],
  rel: (abs: string) => string | null,
): Record<string, unknown> {
  const result = testCase.result();
  const diagnostic = testCase.diagnostic();
  const mode = testCase.options.mode;
  const fails = testCase.options.fails === true;
  let outcome: string | null;
  switch (result.state) {
    case "passed":
      outcome = fails ? "xfailed" : "passed";
      break;
    case "failed":
      outcome = "failed";
      break;
    case "skipped":
      outcome = "skipped";
      break;
    default:
      outcome = null;
  }
  const meta = testCase.meta() as Record<string, unknown>;
  const markers = new Set<string>(testCase.tags ?? []);
  if (mode === "skip") markers.add("skip");
  if (mode === "todo") markers.add("todo");
  if (fails) markers.add("xfail");
  for (const annotation of testCase.annotations()) {
    if (annotation.type && annotation.type !== "notice") markers.add(annotation.type);
  }
  const invariants = [...new Set([...stringList(meta["invariant"]), ...stringList(meta["invariants"])])];
  const line = testCase.location?.line ?? null;
  let property = meta["property"] === true || markers.has("property");
  if (!property && source !== null && line !== null) {
    const next = lines.find((n) => n > line) ?? source.length + 1;
    property = PROPERTY_CALLS.test(source.slice(line - 1, next - 1).join("\n"));
  }
  let message: string | null = null;
  let failSig: string | null = null;
  if (result.state === "failed" && result.errors.length) {
    const error = result.errors[0]!;
    const name = error.name ?? "Error";
    message = redactMessage(`${name}: ${error.message ?? ""}`);
    const frame = (error.stacks ?? []).find((f) => f.file && rel(f.file) !== null);
    const where = frame ? `${rel(frame.file)}:${frame.line}` : "";
    failSig = sha16(`${name}\0${where}`);
  }
  const start = diagnostic ? round6(diagnostic.startTime / 1000) : null;
  const duration = diagnostic ? round6(diagnostic.duration / 1000) : null;
  return {
    id,
    file,
    line,
    outcome,
    duration_s: duration,
    failed_phase: outcome === "failed" ? "call" : null,
    fail_sig: failSig,
    message,
    markers: [...markers].filter((m) => m.length > 0 && m.length <= MAX_ID).sort(),
    invariants: invariants.filter((m) => m.length > 0 && m.length <= MAX_ID),
    property,
    start,
    stop: start !== null && duration !== null ? round6(start + duration) : null,
  };
}

export class CoversetReporter implements Reporter {
  private vitest: Vitest | undefined;
  private coverage: Record<string, FileCoverageData> | undefined;
  private root = process.cwd();
  private projectRoot = process.cwd();
  private outDir = "";
  private spoolDir = "";
  /** Why this reporter writes nothing (set once, warned once). */
  private disabled: string | undefined;

  constructor(private readonly options: CoversetReporterOptions = {}) {}

  onInit(vitest: Vitest): void {
    this.vitest = vitest;
    this.projectRoot = vitest.config.root;
    this.root = repoRoot(this.projectRoot, this.options.root);
    this.outDir = resolve(this.projectRoot, this.options.outDir ?? process.env[OUT_ENV] ?? join(".coverset", "neutral"));
    const fromEnv = process.env[SPOOL_ENV];
    const base = resolve(this.projectRoot, fromEnv && fromEnv !== SPOOL_OFF ? fromEnv : join(".coverset", "spool"));
    this.spoolDir = join(base, `run-${process.pid}-${randomBytes(4).toString("hex")}`);
    try {
      mkdirSync(this.spoolDir, { recursive: true });
    } catch (error) {
      this.disable(`cannot create the spool directory ${this.spoolDir} (${String(error)})`);
      return;
    }
    process.env[SPOOL_ENV] = this.spoolDir;
    removeStaleRuns(base);
  }

  onTestRunStart(): void {
    this.coverage = undefined;
    if (this.disabled === undefined) this.clearSpool();
  }

  private disable(reason: string): void {
    if (this.disabled !== undefined) return;
    this.disabled = reason;
    process.env[SPOOL_ENV] = SPOOL_OFF;
    console.warn(`coverset-vitest: ${reason}: no bundle will be written`);
  }

  onCoverage(coverage: unknown): void {
    const map = coverage as { toJSON?: () => unknown } | undefined;
    const data = map?.toJSON !== undefined ? map.toJSON() : map;
    this.coverage = JSON.parse(JSON.stringify(data ?? {})) as Record<string, FileCoverageData>;
  }

  /** Remove the spool files of an earlier run in watch mode (only the names workers write). */
  private clearSpool(): void {
    try {
      mkdirSync(this.spoolDir, { recursive: true });
      for (const name of readdirSync(this.spoolDir)) {
        if (SPOOL_FILE.test(name)) rmSync(join(this.spoolDir, name), { force: true });
      }
    } catch (error) {
      this.disable(`cannot use the spool directory ${this.spoolDir} (${String(error)})`);
    }
  }

  /** Remove this run's spool directory (it holds only the workers' spool files). */
  private removeSpool(): void {
    try {
      for (const name of readdirSync(this.spoolDir)) {
        if (SPOOL_FILE.test(name)) rmSync(join(this.spoolDir, name), { force: true });
      }
      rmdirSync(this.spoolDir);
    } catch {
      // already gone, or not empty: left for the next run's stale-run sweep
    }
  }

  async onTestRunEnd(testModules: ReadonlyArray<TestModule>): Promise<void> {
    if (this.disabled !== undefined) return;
    if (this.coverage === undefined) {
      // Coverage switched off for this run (`--coverage.enabled=false`): say so, fail nothing.
      console.warn(
        'coverset-vitest: no coverage map reached the reporter, so no bundle was written: enable coverage with the istanbul provider (test.coverage: { enabled: true, provider: "istanbul", include: [...] })',
      );
      this.removeSpool();
      return;
    }
    try {
      await this.write(testModules);
    } catch (error) {
      console.error(`coverset-vitest: the neutral bundle was not written: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}`);
      process.exitCode = 1;
    } finally {
      this.removeSpool();
    }
  }

  private rel(abs: string): string | null {
    const path = relPath(this.root, abs);
    return path === null || path.split("/").includes("node_modules") ? null : path;
  }

  private readSpools(): (SpoolCov | SpoolShapes)[] {
    const rows: (SpoolCov | SpoolShapes)[] = [];
    let names: string[] = [];
    try {
      names = readdirSync(this.spoolDir).filter((n) => SPOOL_FILE.test(n)).sort();
    } catch {
      return rows;
    }
    for (const name of names) {
      for (const line of readFileSync(join(this.spoolDir, name), "utf8").split("\n")) {
        if (!line.trim()) continue;
        try {
          rows.push(JSON.parse(line) as SpoolCov | SpoolShapes);
        } catch {
          // a worker killed mid-write leaves a torn last line
        }
      }
    }
    return rows;
  }

  private async write(testModules: ReadonlyArray<TestModule>): Promise<void> {
    if (this.coverage === undefined) return;
    const warnings: string[] = [];
    const dropped: Record<string, number> = {};
    const drop = (code: string, n = 1): void => {
      dropped[code] = (dropped[code] ?? 0) + n;
    };

    // tests.jsonl -------------------------------------------------------------------------------
    const taskToId = new Map<string, string>();
    const testRows: Record<string, unknown>[] = [];
    const links: { test: string; invariant: string }[] = [];
    const modules: { module: TestModule; file: string; cases: TestCase[] }[] = [];
    for (const module of testModules) {
      const file = this.rel(module.moduleId);
      if (file === null) {
        drop("tests.path");
        continue;
      }
      modules.push({ module, file, cases: [...module.children.allTests()] });
    }
    const ids = uniqueIds(modules.flatMap(({ file, cases }) => cases.map((c) => `${file} > ${c.fullName}`)));
    let next = 0;
    for (const { module, file, cases } of modules) {
      let source: string[] | null = null;
      const lines = cases.map((c) => c.location?.line).filter((n): n is number => n !== undefined).sort((a, b) => a - b);
      for (const testCase of cases) {
        const id = ids[next++]!;
        taskToId.set(testCase.id, id);
        source ??= this.readLines(module.moduleId);
        const row = testRowOf(testCase, id, file, source, lines, (abs) => this.rel(abs));
        testRows.push(row);
        for (const invariant of row["invariants"] as string[]) links.push({ test: id, invariant });
      }
    }
    testRows.sort((a, b) => (String(a["id"]) < String(b["id"]) ? -1 : String(a["id"]) > String(b["id"]) ? 1 : 0));

    // files.jsonl and arcs.jsonl ----------------------------------------------------------------
    const files = new Map<string, StaticFile>();
    const byAbs = new Map<string, StaticFile>();
    for (const data of Object.values(this.coverage)) {
      const path = this.rel(data.path);
      if (path === null) {
        drop("files.outside_root");
        continue;
      }
      const info = staticFile(data, path);
      files.set(path, info);
      byAbs.set(resolve(data.path), info);
    }

    // coverage.jsonl from the spools -------------------------------------------------------------
    const spools = this.readSpools();
    const cells = new Map<string, { test: string; file: string; lines: Set<number>; arcs: Map<string, [number, number]> }>();
    const dumps: SpoolShapes[] = [];
    for (const row of spools) {
      if (row.k === "shapes") {
        dumps.push(row);
        continue;
      }
      if (row.k !== "cov") continue;
      const test = row.task === null ? IMPORT_ID : taskToId.get(row.task);
      if (test === undefined) {
        drop("coverage.unknown_test");
        continue;
      }
      for (const [abs, delta] of Object.entries(row.files)) {
        const info = byAbs.get(resolve(abs));
        if (info === undefined) {
          drop("coverage.unmeasured_file");
          continue;
        }
        const key = `${test}\0${info.path}`;
        let cell = cells.get(key);
        if (cell === undefined) {
          cell = { test, file: info.path, lines: new Set(), arcs: new Map() };
          cells.set(key, cell);
        }
        for (const line of delta.l) {
          if (info.statements.has(line)) cell.lines.add(line);
          else drop("coverage.not_statement");
        }
        for (const [a, b] of delta.a) {
          const k = arcKey(a, b);
          if (info.branches.has(k)) cell.arcs.set(k, [a, b]);
          else drop("coverage.arc_not_branch");
        }
      }
    }
    const coverageRows = [...cells.values()]
      .filter((c) => c.lines.size > 0)
      .sort((x, y) => (x.test === y.test ? (x.file < y.file ? -1 : 1) : x.test < y.test ? -1 : 1))
      .map((c) => ({
        test: c.test,
        file: c.file,
        lines: [...c.lines].sort((a, b) => a - b),
        ...(c.arcs.size ? { arcs: sortArcs(c.arcs.values()) } : {}),
      }));

    const paths = [...files.keys()].sort();
    const fileRows = paths.map((path) => {
      const info = files.get(path)!;
      return {
        path,
        n_lines: info.nLines,
        statements: [...info.statements].sort((a, b) => a - b),
        excluded: [],
        branches: sortArcs(info.branches.values()),
      };
    });
    const arcRows = paths
      .filter((p) => files.get(p)!.executedArcs.size > 0)
      .map((p) => ({ file: p, arcs: sortArcs(files.get(p)!.executedArcs.values()) }));

    // functions.jsonl ---------------------------------------------------------------------------
    const functionRows = await this.functions(files, warnings);

    // shapes.jsonl ------------------------------------------------------------------------------
    const { rows: shapeRows, errors: recorderErrors, reattributed } = this.shapes(dumps, taskToId, files, drop);

    // write ---------------------------------------------------------------------------------------
    const manifest = {
      format: "coverset-neutral",
      version: 1,
      language: "typescript",
      producer: { name: PRODUCER, version: VERSION },
      created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
      shard: this.shard(),
      meta: {
        runner: "vitest",
        vitest: this.vitest?.version ?? null,
        node: process.versions.node,
        typescript_api: (await import("typescript")).default.version,
        coverage: "istanbul (per-test counter deltas)",
        recorder_errors: recorderErrors,
        reattributed_callers: reattributed,
        dropped,
        warnings,
      },
    };
    this.writeBundle({
      "tests.jsonl": jsonl(testRows),
      "files.jsonl": jsonl(fileRows),
      "coverage.jsonl": jsonl(coverageRows),
      "arcs.jsonl": jsonl(arcRows),
      "functions.jsonl": jsonl(functionRows),
      "shapes.jsonl": jsonl(shapeRows),
      "links.jsonl": jsonl(links),
      "manifest.json": toJson(manifest, 1) + "\n",
    });
    const summary = `${testRows.length} tests, ${fileRows.length} files, ${coverageRows.length} coverage rows, ${functionRows.length} functions, ${shapeRows.length} shape rows`;
    console.log(`coverset-vitest: wrote ${this.outDir} (${summary})`);
    for (const warning of warnings) console.warn(`coverset-vitest: ${warning}`);
  }

  private readLines(abs: string): string[] | null {
    try {
      return readFileSync(abs, "utf8").split(/\r\n|\r|\n/);
    } catch {
      return null;
    }
  }

  private async functions(files: Map<string, StaticFile>, warnings: string[]): Promise<object[]> {
    const { functionRows, createProgram } = await import("./functions.js");
    const { parseSource } = await import("./regions.js");
    const sources = [...files.values()];
    const absToRel = new Map(sources.map((f) => [resolve(f.abs), f.path]));
    let program: import("typescript").Program | undefined;
    let checker: import("typescript").TypeChecker | undefined;
    if (this.options.types !== false) {
      try {
        program = createProgram(sources.map((f) => f.abs), this.projectRoot);
        checker = program.getTypeChecker();
      } catch (error) {
        warnings.push(`no types: the TypeScript program could not be built (${String(error)})`);
      }
    }
    const relOf = (fileName: string): string | undefined => {
      const known = absToRel.get(resolve(fileName));
      if (known !== undefined) return known;
      const rel = this.rel(resolve(fileName));
      return rel === null || rel.endsWith(".d.ts") ? undefined : rel;
    };
    const rows: object[] = [];
    for (const info of sources.sort((a, b) => (a.path < b.path ? -1 : 1))) {
      let sf = program?.getSourceFile(resolve(info.abs));
      let fileChecker = checker;
      if (sf === undefined) {
        try {
          sf = parseSource(info.abs, readFileSync(info.abs, "utf8"));
          fileChecker = undefined;
        } catch {
          warnings.push(`${info.path}: unreadable, no function regions`);
          continue;
        }
      }
      try {
        rows.push(...functionRows(sf, info.path, fileChecker, relOf));
      } catch (error) {
        warnings.push(`${info.path}: regions failed (${String(error)})`);
      }
    }
    return rows;
  }

  private shapes(
    dumps: SpoolShapes[],
    taskToId: Map<string, string>,
    files: Map<string, StaticFile>,
    drop: (code: string, n?: number) => void,
  ): { rows: object[]; errors: number; reattributed: number } {
    type Triple = [string, string, number];
    const funcs = new Map<string, { func: Triple; calls: number; tests: Set<string>; truncated: boolean; cap: string | null }>();
    const obs = new Map<
      string,
      { func: Triple; param: string; shape: string; caller: Triple | { test: string }; samples: number; tests: Set<string>; example: string | null; exampleTest: string | null }
    >();
    let errors = 0;
    let reattributed = 0;
    const tripleKey = (t: Triple): string => `${t[0]}\0${t[1]}\0${t[2]}`;
    for (const dump of dumps) {
      errors += dump.errors ?? 0;
      const byUid = new Map<number, Triple>();
      for (const f of dump.funcs) {
        if (!files.has(f.path)) {
          drop("shapes.unmeasured_file");
          continue;
        }
        const func: Triple = [f.path, f.qualname, f.line];
        byUid.set(f.uid, func);
        const key = tripleKey(func);
        const entry = funcs.get(key) ?? { func, calls: 0, tests: new Set<string>(), truncated: false, cap: null };
        entry.calls += f.calls;
        for (const t of f.tests) {
          const id = taskToId.get(t);
          if (id !== undefined) entry.tests.add(id);
        }
        if (f.capped) {
          entry.truncated = true;
          entry.cap ??= f.cap;
        }
        funcs.set(key, entry);
      }
      for (const o of dump.obs) {
        const func = byUid.get(o.f);
        if (func === undefined) continue;
        let caller: Triple | { test: string };
        if ("test" in o.c) {
          const id = taskToId.get(o.c.test);
          if (id === undefined) {
            drop("shapes.unknown_test");
            continue;
          }
          caller = { test: id };
        } else {
          const callerFunc = byUid.get(o.c.func);
          if (callerFunc !== undefined) {
            caller = callerFunc;
          } else {
            // The caller is instrumented but not measured (outside coverage.include): as for
            // pytest-coverset, where only measured code is traced, the call comes from the test.
            // The recorder aggregates per test, so the row's one test is the caller.
            const id = o.t.length === 1 ? taskToId.get(o.t[0]!) : undefined;
            if (id === undefined) {
              drop("shapes.unmeasured_caller");
              continue;
            }
            caller = { test: id };
            reattributed++;
          }
        }
        const tests = o.t.map((t) => taskToId.get(t)).filter((t): t is string => t !== undefined);
        if (!tests.length) continue;
        const callerKey = "test" in caller ? `t\0${caller.test}` : `f\0${tripleKey(caller)}`;
        const key = `${tripleKey(func)}\0${o.p}\0${o.s}\0${callerKey}`;
        const entry = obs.get(key) ?? { func, param: o.p, shape: o.s, caller, samples: 0, tests: new Set<string>(), example: null, exampleTest: null };
        entry.samples += o.n;
        for (const t of tests) entry.tests.add(t);
        const exampleTest = o.ext === null ? null : (taskToId.get(o.ext) ?? null);
        if (o.ex !== null && exampleTest !== null) {
          // Workers finish in any order: keep the shortest, then smallest, example (deterministic).
          const better =
            entry.example === null ||
            o.ex.length < entry.example.length ||
            (o.ex.length === entry.example.length && (o.ex < entry.example || (o.ex === entry.example && exampleTest < entry.exampleTest!)));
          if (better) {
            entry.example = o.ex.slice(0, MAX_EXAMPLE);
            entry.exampleTest = exampleTest;
          }
        }
        obs.set(key, entry);
      }
    }
    const rows: object[] = [];
    const sortedObs = [...obs.values()].sort((a, b) => {
      const ka = JSON.stringify([a.func, a.param, a.shape, a.caller]);
      const kb = JSON.stringify([b.func, b.param, b.shape, b.caller]);
      return ka < kb ? -1 : ka > kb ? 1 : 0;
    });
    for (const o of sortedObs) {
      const sampling = funcs.get(tripleKey(o.func));
      rows.push({
        kind: "obs",
        func: o.func,
        param: o.param,
        shape: o.shape,
        caller: o.caller,
        samples: o.samples,
        tests: [...o.tests].sort(),
        truncated: sampling?.truncated ?? false,
        example: o.example,
        example_test: o.exampleTest,
      });
    }
    for (const f of [...funcs.values()].sort((a, b) => (tripleKey(a.func) < tripleKey(b.func) ? -1 : 1))) {
      rows.push({
        kind: "sampling",
        func: f.func,
        calls: f.calls,
        tests: [...f.tests].sort(),
        truncated: f.truncated,
        cap: f.truncated ? f.cap : null,
      });
    }
    return { rows, errors, reattributed };
  }

  private shard(): number | null {
    const env = process.env[SHARD_ENV];
    if (env !== undefined && /^[0-9]{1,7}$/.test(env.trim())) return Number(env.trim());
    const shard = (this.vitest?.config as { shard?: { index: number; count: number } } | undefined)?.shard;
    return shard ? shard.index - 1 : null;
  }

  private writeBundle(contents: Record<string, string>): void {
    mkdirSync(this.outDir, { recursive: true });
    for (const name of BUNDLE_FILES) rmSync(join(this.outDir, name), { force: true });
    // Data files first, the manifest last, each through a temporary name.
    const names = Object.keys(contents).sort((a, b) => (a === "manifest.json" ? 1 : b === "manifest.json" ? -1 : 0));
    for (const name of names) {
      const tmp = join(this.outDir, `.${name}.tmp`);
      writeFileSync(tmp, contents[name]!, "utf8");
      renameSync(tmp, join(this.outDir, name));
    }
  }
}

/** Remove `run-<pid>-*` spool directories under `base` whose process no longer exists (a killed run). */
function removeStaleRuns(base: string): void {
  let names: string[];
  try {
    names = readdirSync(base);
  } catch {
    return;
  }
  for (const name of names) {
    const match = RUN_DIR.exec(name);
    if (match === null || Number(match[1]) === process.pid) continue;
    try {
      process.kill(Number(match[1]), 0);
      continue; // alive (or someone else's): leave it
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") continue;
    }
    const dir = join(base, name);
    try {
      for (const file of readdirSync(dir)) if (SPOOL_FILE.test(file)) rmSync(join(dir, file), { force: true });
      rmdirSync(dir);
    } catch {
      // not ours to force
    }
  }
}

/** The Vitest reporter that writes the neutral bundle. */
export function coversetReporter(options: CoversetReporterOptions = {}): Reporter {
  return new CoversetReporter(options);
}
