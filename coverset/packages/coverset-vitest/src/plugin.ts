/**
 * `coversetPlugin()`: the Vite plugin that instruments the measured sources for the shape
 * recorder (`instrument.ts`) and wires the setup file and the Istanbul instrumenter into Vitest's
 * config.
 *
 * Measured sources are the files under `include` (directories relative to Vite's root; default:
 * the directories `test.coverage.include` names, else every file under the root) with a
 * JavaScript or TypeScript extension, outside `node_modules` and not test files (`*.test.*`,
 * `*.spec.*`, `__tests__/`). The plugin runs before Vite's own TypeScript transform
 * (`enforce: "pre"`), so it sees the original source and its line numbers.
 *
 * Istanbul: `@vitest/coverage-istanbul` instruments every file twice, once with Vite's combined
 * source map (whose code it keeps) and once with an identity map stripped of `file`,
 * `sourceRoot` and `ignoreList` (whose source map it keeps). The coverage data Istanbul embeds at
 * the top of the code holds the input map, so when the combined map carries those three fields
 * (it does as soon as a plugin such as this one returns a map) the kept code has more lines than
 * the kept map describes, and stack traces from measured files point below the right line. The
 * plugin therefore sets `test.coverage.instrumenter` to a wrapper of Vitest's instrumenter (or
 * of the one configured) that drops those fields (and any other extension field) from every input map: both passes then emit
 * the same code, and runtime lines are the original ones with and without the plugin.
 */
import { createRequire } from "node:module";
import { join, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import type { Plugin } from "vite";

import { cleanId, isTestFile, relPath, repoRoot } from "./paths.js";

/** `COVERSET_SHAPES=0` turns the shape instrumentation off (coverage and tests are still recorded). */
export const SHAPES_ENV = "COVERSET_SHAPES";

/** Absolute path of the setup file, for `test.setupFiles`. */
export const coversetSetup: string = fileURLToPath(new URL("./setup.js", import.meta.url));

export interface CoversetPluginOptions {
  /**
   * Directories (relative to Vite's root) whose files are instrumented; default: the directories
   * `test.coverage.include` names (`src/**\/*.ts` → `src`), else all of the root.
   */
  include?: string[];
  /** Directories or files (relative to Vite's root) never instrumented. */
  exclude?: string[];
  /** Directory bundle paths are relative to (default: `COVERSET_ROOT`, else the git work tree). */
  root?: string;
  /** Set false to record coverage only (no shape instrumentation). */
  shapes?: boolean;
}

const SOURCE_EXT = /\.(?:[cm]?[jt]s|[jt]sx)$/;

function within(file: string, dirs: string[]): boolean {
  return dirs.some((dir) => file === dir || file.startsWith(dir.endsWith(sep) ? dir : dir + sep));
}

/**
 * The directories (or files) a list of coverage globs can match: each pattern's leading segments
 * before the first one with a glob character (`src/**\/*.ts` → `src`, `**\/*.ts` → `.`). Negated
 * patterns are skipped; undefined when there is no positive pattern.
 */
export function globRoots(patterns: unknown): string[] | undefined {
  const list = typeof patterns === "string" ? [patterns] : Array.isArray(patterns) ? patterns : [];
  const roots = new Set<string>();
  for (const pattern of list) {
    if (typeof pattern !== "string" || !pattern.trim() || pattern.startsWith("!")) continue;
    const segments = pattern.replace(/^\.\//, "").split("/");
    const fixed: string[] = [];
    for (const segment of segments) {
      if (/[*?[\]{}()!+@]/.test(segment)) break;
      fixed.push(segment);
    }
    roots.add(fixed.length ? fixed.join("/") : ".");
  }
  return roots.size ? [...roots] : undefined;
}

type MapLike = Record<string, unknown> | string | null | undefined;

interface Instrumenter {
  instrumentSync: (code: string, filename: string, inputSourceMap?: MapLike) => string;
  lastSourceMap: () => unknown;
  lastFileCoverage: () => unknown;
}

type InstrumenterFactory = (options: Record<string, unknown>) => Instrumenter;

/** The fields of the identity map Vitest's second Istanbul pass gets (after it deletes the others). */
const MAP_FIELDS = ["version", "sources", "sourcesContent", "names", "mappings"];

/**
 * A copy of a source map with only {@link MAP_FIELDS}: `file`, `sourceRoot`, `ignoreList` (and
 * any other extension field) are what only the first of Istanbul's two passes would see.
 */
export function stripMap(map: MapLike): MapLike {
  if (map === null || typeof map !== "object") return map;
  const out: Record<string, unknown> = {};
  for (const key of MAP_FIELDS) if (key in map) out[key] = map[key];
  return out;
}

/** `factory`'s instrumenter, with every input source map passed through {@link stripMap}. */
export function wrapInstrumenterFactory(factory: InstrumenterFactory): InstrumenterFactory {
  return (options) => {
    const inner = factory(options);
    return {
      instrumentSync: (code, filename, inputSourceMap) => inner.instrumentSync(code, filename, stripMap(inputSourceMap)),
      lastSourceMap: () => inner.lastSourceMap(),
      lastFileCoverage: () => inner.lastFileCoverage(),
    };
  };
}

/** Vitest's default Istanbul instrumenter (`@vitest/coverage-istanbul`'s own options), or undefined. */
async function defaultInstrumenterFactory(root: string): Promise<InstrumenterFactory | undefined> {
  for (const base of [join(root, "package.json"), fileURLToPath(import.meta.url)]) {
    try {
      const provider = createRequire(base).resolve("@vitest/coverage-istanbul/package.json");
      const path = createRequire(provider).resolve("@vitest/istanbul-lib-instrument");
      const lib = (await import(pathToFileURL(path).href)) as { createInstrumenter: (o: Record<string, unknown>) => Instrumenter };
      return (options) =>
        lib.createInstrumenter({
          produceSourceMap: true,
          autoWrap: false,
          esModules: true,
          compact: false,
          ignoreLines: true,
          ...options,
        });
    } catch {
      // not installed next to this root: try the next base
    }
  }
  return undefined;
}

/** `setupFiles` with the coverset setup file first (moved there when listed elsewhere). */
export function setupFirst(setupFiles: unknown, root = process.cwd()): string[] {
  const list = setupFiles === undefined ? [] : ([] as unknown[]).concat(setupFiles).filter((f): f is string => typeof f === "string");
  const others = list.filter((f) => resolve(root, f) !== coversetSetup);
  return [coversetSetup, ...others];
}

export function coversetPlugin(options: CoversetPluginOptions = {}): Plugin {
  let viteRoot = process.cwd();
  let root = viteRoot;
  let include: string[] = [];
  let exclude: string[] = [];
  return {
    name: "coverset-vitest",
    enforce: "pre",
    async config(config) {
      const viteConfigRoot = resolve(config.root ?? process.cwd());
      const test = ((config as { test?: Record<string, unknown> }).test ??= {});
      test["setupFiles"] = setupFirst(test["setupFiles"], viteConfigRoot);
      test["includeTaskLocation"] ??= true;
      const coverage = test["coverage"];
      if (coverage !== null && typeof coverage === "object") {
        const cov = coverage as Record<string, unknown>;
        if (cov["provider"] === undefined || cov["provider"] === "istanbul") {
          const configured = cov["instrumenter"];
          const factory =
            typeof configured === "function" ? (configured as InstrumenterFactory) : await defaultInstrumenterFactory(viteConfigRoot);
          if (factory !== undefined) cov["instrumenter"] = wrapInstrumenterFactory(factory);
          else console.warn("coverset-vitest: @vitest/istanbul-lib-instrument not found: stack traces of measured files may point at shifted lines");
        }
      }
    },
    configResolved(config) {
      viteRoot = config.root;
      root = repoRoot(viteRoot, options.root);
      const coverageInclude = (config as { test?: { coverage?: { include?: unknown } } }).test?.coverage?.include;
      include = (options.include ?? globRoots(coverageInclude) ?? ["."]).map((d) => resolve(viteRoot, d));
      exclude = (options.exclude ?? []).map((d) => resolve(viteRoot, d));
    },
    async transform(code, id) {
      if (options.shapes === false || process.env[SHAPES_ENV] === "0" || id.startsWith("\0")) return null;
      const file = cleanId(id);
      if (!SOURCE_EXT.test(file) || file.endsWith(".d.ts") || file.split(/[\\/]/).includes("node_modules")) return null;
      if (!within(file, include) || within(file, exclude)) return null;
      const fileId = relPath(root, file);
      if (fileId === null || isTestFile(fileId)) return null;
      const { instrument } = await import("./instrument.js");
      const result = instrument(code, file, fileId);
      return result === null ? null : { code: result.code, map: result.map };
    },
  };
}
