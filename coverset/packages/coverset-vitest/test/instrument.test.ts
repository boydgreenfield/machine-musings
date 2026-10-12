/**
 * The instrumentation plugin on a sample file: the output still parses, every original line keeps
 * its number, the program behaves the same, and the recorder sees arguments, returns, callers and
 * receivers.
 */
import ts from "typescript";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { instrument } from "../src/instrument.js";
import { Recorder } from "../src/recorder.js";

const SOURCE = `"use strict"
export enum Currency { USD = "USD", EUR = "EUR" }

export function price(cents: number, currency: Currency = Currency.USD): string {
  if (cents < 0) return "-" + price(-cents, currency)
  return currency + (cents / 100).toFixed(2)
}

export class Base {
  constructor(public name: string) {}
}

export class Cart extends Base {
  items: number[] = [];
  label = () => this.name.toUpperCase();
  constructor(name: string) {
    super(name);
  }
  add(qty: number) {
    this.items.push(qty);
    return this;
  }
  get size(): number { return this.items.length }
  static empty(): Cart { return new Cart("empty") }
}

export function* counter(limit: number) {
  for (let i = 0; i < limit; i++) yield i;
  return limit;
}

export async function later(value: string): Promise<string> {
  await Promise.resolve();
  return value.trim();
}

export const pick = ({ a }: { a: number }, ...rest: number[]) => a + rest.length;
export const curry = (a: number) => (b: number) => a + b;
export const obj = (k: string) => ({ [k]: 1 });

export function bare(flag: boolean) {
  if (flag) return;
  return
}

export function sum(xs: number[]) {
  return xs.reduce((s, x) => s + x, 0), xs.length;
}

export function strict(n: number) {
  "use strict"
  return n
}

export const atImport = price(100);
`;

function transpile(code: string): string {
  const out = ts.transpileModule(code, {
    reportDiagnostics: true,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  expect(out.diagnostics ?? []).toEqual([]);
  return out.outputText;
}

function load(code: string): Record<string, any> {
  const exports: Record<string, any> = {};
  new Function("exports", "require", transpile(code))(exports, () => ({}));
  return exports;
}

function tokens(line: string): string[] {
  return line.match(/\w+|[^\s\w]/g) ?? [];
}

function isSubsequence(small: string[], big: string[]): boolean {
  let i = 0;
  for (const t of big) if (i < small.length && t === small[i]) i++;
  return i === small.length;
}

describe("instrument", () => {
  const result = instrument(SOURCE, "/repo/src/cart.ts", "src/cart.ts")!;

  it("keeps every line where it was and still parses", () => {
    const before = SOURCE.split("\n");
    const after = result.code.split("\n");
    expect(after.length).toBe(before.length);
    before.forEach((line, i) => expect(isSubsequence(tokens(line), tokens(after[i]!)), `line ${i + 1}`).toBe(true));
    const sf = ts.createSourceFile("x.ts", result.code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
    expect((sf as unknown as { parseDiagnostics: unknown[] }).parseDiagnostics).toEqual([]);
    expect(result.regions).toBe(17);
    expect(result.map.mappings.length).toBeGreaterThan(0);
  });

  it("marks every added statement for Istanbul to ignore", () => {
    const added = result.code.match(/(?:const __cs\$\d+ = |; )?\/\* istanbul ignore next -- @preserve \*\//g) ?? [];
    const statements = result.code.match(/globalThis\.__coverset__\.enter\(/g) ?? [];
    // Every enter call sits in an ignored statement except those of expression-bodied arrows.
    expect(added.length).toBeGreaterThan(0);
    expect(statements.length).toBe(17);
  });

  describe("behaviour", () => {
    let recorder: Recorder;
    beforeEach(() => {
      recorder = new Recorder();
      globalThis.__coverset__ = recorder;
    });
    afterEach(() => {
      globalThis.__coverset__ = undefined;
    });

    it("computes the same results as the original", async () => {
      const plain = load(SOURCE);
      const traced = load(result.code);
      recorder.setTest("t1");
      for (const m of [plain, traced]) {
        expect(m.price(1250, m.Currency.EUR)).toBe("EUR12.50");
        expect(m.price(-5)).toBe("-USD0.05");
        const cart = new m.Cart("bob").add(2).add(3);
        expect([cart.size, cart.label(), m.Cart.empty().name]).toEqual([2, "BOB", "empty"]);
        expect([...m.counter(3)]).toEqual([0, 1, 2]);
        expect(await m.later("  x ")).toBe("x");
        expect(m.pick({ a: 1 }, 2, 3)).toBe(3);
        expect(m.curry(1)(2)).toBe(3);
        expect(m.obj("k")).toEqual({ k: 1 });
        expect(m.bare(true)).toBeUndefined();
        expect(m.bare(false)).toBeUndefined();
        expect(m.sum([1, 2])).toBe(2);
        expect(m.atImport).toBe("USD1.00");
        expect(m.strict(4)).toBe(4);
      }
      expect(recorder.errors).toBe(0);
    });

    it("records arguments, returns, callers, receivers and enum members", () => {
      const m = load(result.code);
      recorder.setTest("t1");
      m.price(1250, m.Currency.EUR);
      m.price(-5);
      new m.Cart("bob").add(2);
      m.sum([1, 2]);
      const dump = recorder.dump();
      const funcs = new Map(dump.funcs.map((f) => [f.uid, f]));
      const rows = dump.obs.map((o) => ({
        func: funcs.get(o.f)!.qualname,
        param: o.p,
        shape: o.s,
        caller: "test" in o.c ? `test:${o.c.test}` : funcs.get(o.c.func)!.qualname,
        example: o.ex,
      }));
      expect(rows).toContainEqual({ func: "price", param: "currency", shape: "str|ascii|len:2-10", caller: "test:t1", example: "Currency.EUR" });
      expect(rows).toContainEqual({ func: "price", param: "cents", shape: "int|pos", caller: "price", example: "5" });
      expect(rows).toContainEqual({ func: "price", param: "return", shape: "str|ascii|digits|len:2-10", caller: "test:t1", example: '"EUR12.50"' });
      expect(rows).toContainEqual({ func: "Cart.add", param: "<receiver>", shape: "obj:src.cart.Cart", caller: "test:t1", example: null });
      expect(rows).toContainEqual({ func: "Cart.constructor", param: "<receiver>", shape: "obj:src.cart.Cart", caller: "test:t1", example: null });
      expect(rows).toContainEqual({ func: "sum.<anonymous>", param: "x", shape: "int|pos", caller: "sum", example: "1" });
      // price(100) ran at import, outside any test: counted, not written.
      expect(dump.funcs.find((f) => f.qualname === "price")!.calls).toBe(4);
      expect(rows.filter((r) => r.caller === "test:null")).toEqual([]);
    });

    it("caps sampling after 64 calls with nothing new", () => {
      const m = load(result.code);
      recorder.setTest("t1");
      for (let i = 0; i < 500; i++) m.curry(1);
      const curry = recorder.dump().funcs.find((f) => f.qualname === "curry")!;
      expect(curry.capped).toBe(true);
      expect(curry.cap).toBe("sample");
      expect(curry.calls).toBe(65);
    });

    it("never throws into the program when recording fails", () => {
      const m = load(result.code);
      recorder.setTest("t1");
      const hostile = new Proxy({}, { get: () => { throw new Error("boom"); }, getPrototypeOf: () => { throw new Error("boom"); } });
      expect(m.curry(hostile as unknown as number)).toBeTypeOf("function");
    });
  });
});
