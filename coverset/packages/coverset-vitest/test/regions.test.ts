/** Function regions and type descriptors of a small sample (`test/fixtures/sample.ts`). */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { createProgram, functionRows, type FunctionRow } from "../src/functions.js";
import { indexRegions, parseSource } from "../src/regions.js";

const SAMPLE = fileURLToPath(new URL("./fixtures/sample.ts", import.meta.url));
const PATH = "pkg/sample.ts";

function rows(): Map<string, FunctionRow> {
  const program = createProgram([SAMPLE], fileURLToPath(new URL("./fixtures/", import.meta.url)));
  const checker = program.getTypeChecker();
  const sf = program.getSourceFile(SAMPLE)!;
  const out = functionRows(sf, PATH, checker, (f) => (f === SAMPLE ? PATH : undefined));
  return new Map(out.map((r) => [`${r.qualname}#${r.ordinal}`, r]));
}

describe("regions", () => {
  const sf = parseSource(SAMPLE, readFileSync(SAMPLE, "utf8"));
  const regions = indexRegions(sf);
  const names = regions.map((r) => `${r.qualname}#${r.ordinal}:${r.kind}`);

  it("names functions, methods, accessors, callbacks and bound arrows", () => {
    expect(names).toEqual([
      "<module>#0:module",
      "Money#0:class",
      "Money.constructor#0:method",
      "Money.plus#0:method",
      "Money.euros#0:method",
      "Money.euros#1:method",
      "Money.zero#0:method",
      "scalars#0:function",
      "literals#0:function",
      "containers#0:function",
      "objects#0:function",
      "fetchCount#0:function",
      "outer#0:function",
      "outer.inner#0:function",
      "outer.<anonymous>#0:function",
      "outer.<anonymous>#1:function",
      "api.get#0:function",
      "api.put#0:function",
      "double#0:function",
      "default#0:function",
      "Geometry.area#0:function",
    ]);
  });

  it("puts def_line on the declaration and body_start after a JSDoc", () => {
    const plus = regions.find((r) => r.qualname === "Money.plus")!;
    expect([plus.defLine, plus.bodyStart, plus.endLine]).toEqual([20, 21, 22]);
    const ctor = regions.find((r) => r.qualname === "Money.constructor")!;
    expect([ctor.defLine, ctor.bodyStart, ctor.endLine]).toEqual([17, 17, 17]);
    const params = regions.find((r) => r.qualname === "containers")!;
    expect([params.defLine, params.bodyStart, params.endLine]).toEqual([45, 57, 58]);
    const double = regions.find((r) => r.qualname === "double")!;
    expect([double.defLine, double.bodyStart, double.endLine]).toEqual([82, 82, 82]);
    expect(regions.find((r) => r.qualname === "api.put")!.params.map((p) => p.label)).toEqual(["key", "arg1"]);
  });
});

describe("descriptors", () => {
  const byName = rows();
  const types = (q: string) => byName.get(`${q}#0`)!.types!;

  it("maps scalars, optionals and literals", () => {
    expect(types("scalars")).toEqual({
      n: { kind: "float" },
      s: { kind: "str" },
      b: { kind: "bool" },
      big: { kind: "int" },
      maybe: { kind: "union", arms: [{ kind: "str" }, { kind: "none" }] },
      nothing: { kind: "none" },
      return: { kind: "none" },
    });
    expect(types("literals")).toEqual({
      zone: { kind: "literal", values: ["eu", "world"] },
      digit: { kind: "literal", values: [1, 2, 3] },
      flag: { kind: "literal", values: [true] },
      color: { kind: "union", arms: [{ kind: "none" }, { kind: "enum", name: "pkg.sample.Color", members: ["Red", "Green"] }] },
      low: { kind: "literal", values: ["red"] },
      return: { kind: "none" },
    });
  });

  it("maps containers", () => {
    expect(types("containers")).toEqual({
      xs: { kind: "list", elem: { kind: "float" } },
      ro: { kind: "list", elem: { kind: "str" } },
      pair: { kind: "tuple", elems: [{ kind: "float" }, { kind: "str" }] },
      rest: { kind: "tuple", elem: { kind: "str" } },
      rec: { kind: "dict", key: { kind: "str" }, value: { kind: "float" } },
      map: { kind: "dict", key: { kind: "str" }, value: { kind: "object", name: "pkg.sample.Point" } },
      set: { kind: "set", elem: { kind: "float" } },
      it: { kind: "list", elem: { kind: "float" } },
      bytes: { kind: "bytes" },
      index: { kind: "dict", key: { kind: "str" }, value: { kind: "bool" } },
      return: { kind: "none" },
    });
  });

  it("maps objects, enums, any and unannotated parameters", () => {
    expect(types("objects")).toEqual({
      p: { kind: "object", name: "pkg.sample.Point" },
      m: { kind: "object", name: "pkg.sample.Money" },
      level: { kind: "enum", name: "pkg.sample.Level", members: ["Low", "High"] },
      a: { kind: "any" },
      u: { kind: "any" },
      d: { kind: "object", name: "Date" },
      f: { kind: "object", name: "Function" },
      untyped: null,
      return: { kind: "none" },
    });
    expect(types("fetchCount")).toEqual({
      id: { kind: "union", arms: [{ kind: "str" }, { kind: "float" }] },
      return: { kind: "float" },
    });
    expect(types("Money.plus")).toEqual({ other: { kind: "object", name: "pkg.sample.Money" }, return: { kind: "object", name: "pkg.sample.Money" } });
    expect(byName.get("Money.constructor#0")!.types).toEqual({ cents: { kind: "float" } });
    expect(byName.get("Money#0")!.types).toBeUndefined();
  });

  it("hashes bodies and signatures without layout or comments", () => {
    const a = byName.get("double#0")!;
    expect(a.body_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(a.sig_hash).toMatch(/^[0-9a-f]{64}$/);
    const reformatted = parseSource("x.ts", "export const double = (n: number) =>\n  /* twice */ n   *   2;\n");
    const [row] = functionRows(reformatted, "x.ts", undefined, () => undefined).filter((r) => r.qualname === "double");
    expect(row!.body_hash).toBe(a.body_hash);
    expect(row!.sig_hash).toBe(a.sig_hash);
  });

  it("keeps a method's edit out of its class's body hash", () => {
    const before = parseSource("c.ts", "class A {\n  m() { return 1; }\n}\n");
    const after = parseSource("c.ts", "class A {\n  m() { return 2; }\n}\n");
    const hash = (sf: ReturnType<typeof parseSource>, q: string) =>
      functionRows(sf, "c.ts", undefined, () => undefined).find((r) => r.qualname === q)!.body_hash;
    expect(hash(before, "A")).toBe(hash(after, "A"));
    expect(hash(before, "<module>")).toBe(hash(after, "<module>"));
    expect(hash(before, "A.m")).not.toBe(hash(after, "A.m"));
  });
});
