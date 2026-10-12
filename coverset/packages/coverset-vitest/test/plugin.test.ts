/** The plugin's Vitest wiring: setup order, the Istanbul instrumenter wrapper, the default include. */
import { describe, expect, it } from "vitest";

import { coversetPlugin, coversetSetup, globRoots, setupFirst, stripMap, wrapInstrumenterFactory } from "../src/plugin.js";

describe("setupFirst", () => {
  it("puts the setup file first, moving it when listed later", () => {
    expect(setupFirst(undefined)).toEqual([coversetSetup]);
    expect(setupFirst("./a.ts")).toEqual([coversetSetup, "./a.ts"]);
    expect(setupFirst(["./a.ts", coversetSetup, "./b.ts"])).toEqual([coversetSetup, "./a.ts", "./b.ts"]);
  });
});

describe("globRoots", () => {
  it("takes the fixed leading directories of coverage globs", () => {
    expect(globRoots(["src/**/*.ts", "lib/*.js", "!src/gen/**", "./pkg/a.ts", "**/*.ts"])).toEqual(["src", "lib", "pkg/a.ts", "."]);
    expect(globRoots("src/{a,b}/**")).toEqual(["src"]);
    expect(globRoots(undefined)).toBeUndefined();
    expect(globRoots(["!x/**"])).toBeUndefined();
  });
});

describe("the Istanbul instrumenter", () => {
  const combined = {
    version: 3,
    file: "/repo/src/a.ts",
    sourceRoot: "",
    sources: ["/repo/src/a.ts"],
    sourcesContent: ["export const a = 1;\n"],
    names: [],
    mappings: "AAAA",
    ignoreList: [],
    x_google_ignoreList: [],
  };

  it("drops the fields only the first of Vitest's two passes sees", () => {
    expect(stripMap(combined)).toEqual({ version: 3, sources: ["/repo/src/a.ts"], sourcesContent: ["export const a = 1;\n"], names: [], mappings: "AAAA" });
    expect(stripMap(undefined)).toBeUndefined();
  });

  it("is wired into coverage.instrumenter, and both passes then emit the same code", async () => {
    const plugin = coversetPlugin();
    const config: Record<string, any> = { test: { setupFiles: ["./user.ts", coversetSetup], coverage: { provider: "istanbul", include: ["src/**"] } } };
    await (plugin.config as (c: unknown) => Promise<void>)(config);
    expect(config["test"].setupFiles).toEqual([coversetSetup, "./user.ts"]);
    expect(config["test"].includeTaskLocation).toBe(true);
    const factory = config["test"].coverage.instrumenter as Parameters<typeof wrapInstrumenterFactory>[0];
    expect(typeof factory).toBe("function");
    const options = { coverageVariable: "__C__", coverageGlobalScope: "globalThis", coverageGlobalScopeFunc: false };
    const instrumenter = factory(options);
    const code = "export const a = 1;\n";
    const first = instrumenter.instrumentSync(code, "/repo/src/a.ts", combined);
    const second = instrumenter.instrumentSync(code, "/repo/src/a.ts", stripMap(combined));
    expect(first.split("\n").length).toBe(second.split("\n").length);
    expect(instrumenter.lastSourceMap()).toBeTruthy();
    expect(instrumenter.lastFileCoverage()).toBeTruthy();
  });

  it("wraps an instrumenter the config already names", async () => {
    const seen: unknown[] = [];
    const custom = () => ({ instrumentSync: (code: string, _f: string, map?: unknown) => (seen.push(map), code), lastSourceMap: () => null, lastFileCoverage: () => ({}) });
    const config: Record<string, any> = { test: { coverage: { instrumenter: custom } } };
    await (coversetPlugin().config as (c: unknown) => Promise<void>)(config);
    config["test"].coverage.instrumenter({}).instrumentSync("x", "f", combined);
    expect(seen).toEqual([stripMap(combined)]);
  });

  it("leaves a v8 configuration alone", async () => {
    const config: Record<string, any> = { test: { coverage: { provider: "v8" } } };
    await (coversetPlugin().config as (c: unknown) => Promise<void>)(config);
    expect(config["test"].coverage.instrumenter).toBeUndefined();
  });
});
