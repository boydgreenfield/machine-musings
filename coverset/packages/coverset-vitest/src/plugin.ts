/**
 * `coversetPlugin()`: the Vite plugin that instruments the measured sources for the shape
 * recorder (`instrument.ts`) and wires the setup file into Vitest's config.
 *
 * Measured sources are the files under `include` (directories relative to Vite's root; default:
 * every file under the root) with a JavaScript or TypeScript extension, outside `node_modules`
 * and not test files (`*.test.*`, `*.spec.*`, `__tests__/`). The plugin runs before Vite's own
 * TypeScript transform (`enforce: "pre"`), so it sees the original source and its line numbers.
 */
import { fileURLToPath } from "node:url";
import { resolve, sep } from "node:path";

import type { Plugin } from "vite";

import { cleanId, isTestFile, relPath, repoRoot } from "./paths.js";

/** `COVERSET_SHAPES=0` turns the shape instrumentation off (coverage and tests are still recorded). */
export const SHAPES_ENV = "COVERSET_SHAPES";

/** Absolute path of the setup file, for `test.setupFiles`. */
export const coversetSetup: string = fileURLToPath(new URL("./setup.js", import.meta.url));

export interface CoversetPluginOptions {
  /** Directories (relative to Vite's root) whose files are instrumented; default: all of the root. */
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

export function coversetPlugin(options: CoversetPluginOptions = {}): Plugin {
  let viteRoot = process.cwd();
  let root = viteRoot;
  let include: string[] = [];
  let exclude: string[] = [];
  return {
    name: "coverset-vitest",
    enforce: "pre",
    config(config) {
      const test = ((config as { test?: Record<string, unknown> }).test ??= {});
      const setupFiles = test["setupFiles"] === undefined ? [] : ([] as string[]).concat(test["setupFiles"] as string | string[]);
      if (!setupFiles.some((f) => resolve(f) === coversetSetup)) setupFiles.unshift(coversetSetup);
      test["setupFiles"] = setupFiles;
      test["includeTaskLocation"] ??= true;
    },
    configResolved(config) {
      viteRoot = config.root;
      root = repoRoot(viteRoot, options.root);
      include = (options.include ?? ["."]).map((d) => resolve(viteRoot, d));
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
