/**
 * The shape instrumentation: every function region of a measured file calls the recorder at
 * entry and hands its return values through it.
 *
 * * Block bodies get `const __cs$N = globalThis.__coverset__.enter(...)` right after the opening
 *   brace (after a directive prologue), every `return x` becomes
 *   `return globalThis.__coverset__.leave(__cs$N, (x))`, a bare `return` returns
 *   `leave(__cs$N, undefined)`, and a `leave(__cs$N, undefined)` before the closing brace records a
 *   fall-through. Generators call `enter` only (their `return` ends an iteration).
 * * Expression-bodied arrows become `(...) => globalThis.__coverset__.leave(enter(...), (expr))`:
 *   no statement is added.
 * * Each added statement carries `/* istanbul ignore next -- @preserve *\/`, so Istanbul neither
 *   counts nor instruments it: statements, branches and functions are the same with and without
 *   the plugin (tested). Only text is inserted, never a line break, and the source map is
 *   magic-string's high-resolution map, so every original token keeps its line and column.
 *
 * `enter(fileId, index, qualname, firstLine, spec, flags, args, self)`: `fileId` is the
 * repository-relative path, `index` the region's position in {@link indexRegions}, `spec` a JSON
 * string naming the recorded parameters and the enum of each enum-typed one, `args` the parameter
 * values at entry (after defaults; destructured parameters through `arguments[i]`, not recorded in
 * arrow functions), `self` `this` for class members, `new.target` for constructors.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import MagicString from "magic-string";
import ts from "typescript";

import { indexRegions, parseSource, type Region } from "./regions.js";
import type { EnumSpec } from "./redact.js";

export const GLOBAL = "globalThis.__coverset__";
export const IGNORE = "/* istanbul ignore next -- @preserve */";

export const FLAG_RECEIVER = 1;
export const FLAG_CONSTRUCTOR = 2;
export const FLAG_ASYNC = 4;
export const FLAG_GENERATOR = 8;
export const FLAG_STATIC = 16;

export interface ParamSpec {
  /** Labels of the recorded parameters, in `args` order. */
  p: string[];
  /** Enum declared for a recorded parameter, by its position in `p`. */
  e?: Record<string, EnumSpec>;
}

type EnumTable = Map<string, EnumSpec>;

/** Enums declared in `sf` (members whose value is a literal or an auto-incremented number). */
export function enumsOf(sf: ts.SourceFile): EnumTable {
  const table: EnumTable = new Map();
  for (const statement of sf.statements) {
    if (!ts.isEnumDeclaration(statement)) continue;
    const members: Record<string, string | number> = {};
    let next: number | undefined = 0;
    for (const member of statement.members) {
      const name = ts.isIdentifier(member.name) || ts.isStringLiteral(member.name) ? member.name.text : undefined;
      let value: string | number | undefined;
      const init = member.initializer;
      if (init === undefined) value = next;
      else if (ts.isStringLiteral(init) || ts.isNoSubstitutionTemplateLiteral(init)) value = init.text;
      else if (ts.isNumericLiteral(init)) value = Number(init.text);
      else if (
        ts.isPrefixUnaryExpression(init) &&
        init.operator === ts.SyntaxKind.MinusToken &&
        ts.isNumericLiteral(init.operand)
      ) {
        value = -Number(init.operand.text);
      }
      next = typeof value === "number" ? value + 1 : undefined;
      if (name !== undefined && value !== undefined) members[name] = value;
    }
    if (Object.keys(members).length) table.set(statement.name.text, { name: statement.name.text, members });
  }
  return table;
}

const RESOLVE_EXTENSIONS = ["", ".ts", ".tsx", ".mts", "/index.ts", "/index.tsx"];

/** Finds the enum a parameter's type annotation names: declared in this file or imported from a relative module. */
class EnumResolver {
  private readonly local: EnumTable;
  private readonly imports = new Map<string, { from: string; name: string }>();
  private static readonly cache = new Map<string, EnumTable>();

  constructor(private readonly sf: ts.SourceFile) {
    this.local = enumsOf(sf);
    for (const statement of sf.statements) {
      if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) continue;
      const from = statement.moduleSpecifier.text;
      const bindings = statement.importClause?.namedBindings;
      if (!from.startsWith(".") || bindings === undefined || !ts.isNamedImports(bindings)) continue;
      for (const element of bindings.elements) {
        this.imports.set(element.name.text, { from, name: (element.propertyName ?? element.name).getText(sf) });
      }
    }
  }

  forType(type: ts.TypeNode | undefined): EnumSpec | undefined {
    if (type === undefined) return undefined;
    if (ts.isUnionTypeNode(type)) {
      for (const arm of type.types) {
        const spec = this.forType(arm);
        if (spec !== undefined) return spec;
      }
      return undefined;
    }
    if (!ts.isTypeReferenceNode(type) || !ts.isIdentifier(type.typeName)) return undefined;
    const name = type.typeName.text;
    const local = this.local.get(name);
    if (local !== undefined) return local;
    const imported = this.imports.get(name);
    if (imported === undefined) return undefined;
    const table = this.tableFor(resolve(dirname(this.sf.fileName), imported.from));
    const spec = table?.get(imported.name);
    return spec === undefined ? undefined : { name, members: spec.members };
  }

  private tableFor(base: string): EnumTable | undefined {
    const stripped = base.replace(/\.js$/, "");
    for (const ext of RESOLVE_EXTENSIONS) {
      const candidate = stripped + ext;
      const cached = EnumResolver.cache.get(candidate);
      if (cached !== undefined) return cached;
      if (!existsSync(candidate) || !/\.[cm]?tsx?$/.test(candidate)) continue;
      try {
        const table = enumsOf(parseSource(candidate, readFileSync(candidate, "utf8")));
        EnumResolver.cache.set(candidate, table);
        return table;
      } catch {
        return undefined;
      }
    }
    return undefined;
  }
}

/** Where the entry statement goes: after `{`, or after a directive prologue (then a `;` first). */
function insertionPoint(body: ts.Block): { at: number; lead: string } {
  let at = body.getStart() + 1;
  let lead = "";
  for (const statement of body.statements) {
    if (!ts.isExpressionStatement(statement) || !ts.isStringLiteral(statement.expression)) break;
    at = statement.end;
    lead = ";";
  }
  return { at, lead };
}

/** `return` statements of `fn` itself (not of nested functions or classes). */
function ownReturns(fn: ts.FunctionLikeDeclaration): ts.ReturnStatement[] {
  const out: ts.ReturnStatement[] = [];
  const walk = (node: ts.Node): void => {
    if (ts.isReturnStatement(node)) out.push(node);
    if (ts.isFunctionLike(node) || ts.isClassLike(node)) return;
    ts.forEachChild(node, walk);
  };
  if (fn.body !== undefined) ts.forEachChild(fn.body, walk);
  return out;
}

export interface InstrumentResult {
  code: string;
  map: ReturnType<MagicString["generateMap"]>;
  regions: number;
}

function flagsOf(region: Region): number {
  let flags = 0;
  if (region.hasReceiver) flags |= FLAG_RECEIVER;
  if (region.isConstructor) flags |= FLAG_CONSTRUCTOR;
  if (region.isAsync) flags |= FLAG_ASYNC;
  if (region.isGenerator) flags |= FLAG_GENERATOR;
  if (region.isStatic) flags |= FLAG_STATIC;
  return flags;
}

function enterCall(region: Region, index: number, fileId: string, enums: EnumResolver): string {
  const recorded = region.params.filter((p) => p.kind !== "pattern" || !region.isArrow);
  const spec: ParamSpec = { p: recorded.map((p) => p.label) };
  recorded.forEach((p, i) => {
    const e = enums.forType(p.node.type);
    if (e !== undefined) (spec.e ??= {})[String(i)] = e;
  });
  const args = recorded.map((p) => (p.kind === "pattern" ? `arguments[${p.index}]` : p.ident!)).join(", ");
  const self = region.isConstructor ? "new.target" : region.hasReceiver ? "this" : "undefined";
  return (
    `${GLOBAL}.enter(${JSON.stringify(fileId)}, ${index}, ${JSON.stringify(region.qualname)}, ` +
    `${region.defLine}, ${JSON.stringify(JSON.stringify(spec))}, ${flagsOf(region)}, [${args}], ${self})`
  );
}

/**
 * Instrument `code` (the original source of `fileName`, recorded as `fileId`). Returns null when
 * the file has no function to instrument.
 */
export function instrument(code: string, fileName: string, fileId: string): InstrumentResult | null {
  const sf = parseSource(fileName, code);
  const regions = indexRegions(sf);
  const enums = new EnumResolver(sf);
  const s = new MagicString(code);
  let count = 0;
  // Innermost regions first: insertions that share an index nest correctly (inner closes first).
  for (let index = regions.length - 1; index >= 0; index--) {
    const region = regions[index]!;
    const fn = region.fn;
    if (fn === undefined || fn.body === undefined) continue;
    count++;
    const call = enterCall(region, index, fileId, enums);
    const body = fn.body;
    if (!ts.isBlock(body)) {
      s.prependRight(body.getStart(sf), `${GLOBAL}.leave(${call}, (`);
      s.appendLeft(body.end, "))");
      continue;
    }
    const { at, lead } = insertionPoint(body);
    if (region.isGenerator) {
      s.appendLeft(at, `${lead} ${IGNORE} ${call};`);
      continue;
    }
    const token = `__cs$${index}`;
    s.appendLeft(at, `${lead} ${IGNORE} const ${token} = ${call};`);
    for (const ret of ownReturns(fn)) {
      if (ret.expression !== undefined) {
        s.prependRight(ret.expression.getStart(sf), `${GLOBAL}.leave(${token}, (`);
        s.appendLeft(ret.expression.end, "))");
      } else {
        s.appendLeft(ret.getStart(sf) + "return".length, ` ${GLOBAL}.leave(${token}, undefined)`);
      }
    }
    // The `;` ends a last statement written without one on the same line.
    s.appendLeft(body.end - 1, `; ${IGNORE} ${GLOBAL}.leave(${token}, undefined); `);
  }
  if (count === 0) return null;
  return {
    code: s.toString(),
    map: s.generateMap({ hires: true, source: fileName, includeContent: true }),
    regions: count,
  };
}
