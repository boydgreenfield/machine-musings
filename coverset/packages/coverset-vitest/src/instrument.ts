/**
 * The shape instrumentation: every function region of a measured file calls the recorder at
 * entry and hands its return values through it.
 *
 * * The file starts (on its first line, after a hashbang or directive prologue) with two function
 *   declarations, `__cs$E` (enter) and `__cs$L` (leave), that look the recorder up on the global
 *   object at every call and do nothing when there is none: instrumented code also runs where no
 *   worker loaded the setup file (a Vitest `globalSetup`, a config file, a script importing the
 *   module), and being hoisted function declarations they work before the module body runs (a
 *   function called through a circular import) and whatever the module binds `globalThis` to in
 *   an inner scope. A module that declares `globalThis` at its top level gets the global object
 *   through an indirect `eval("this")` instead, cached on `__cs$E`.
 * * Block bodies get `const __cs$N = __cs$E(...)` right after the opening brace (after a directive
 *   prologue), every `return x` becomes `return __cs$L(__cs$N, (x))`, a bare `return` returns
 *   `__cs$L(__cs$N, undefined)`, and a `__cs$L(__cs$N, undefined)` before the closing brace records
 *   a fall-through. Generators call `__cs$E` only (their `return` ends an iteration).
 * * Expression-bodied arrows become `(...) => __cs$L(__cs$E(...), (expr))`: no statement is added.
 * * Each added statement and declaration carries `/* istanbul ignore next -- @preserve *\/`, so
 *   Istanbul neither counts nor instruments it: statements, branches and functions are the same
 *   with and without the plugin (tested). Only text is inserted, never a line break, and the
 *   source map is magic-string's high-resolution map, so every original token keeps its line and
 *   column.
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

export const IGNORE = "/* istanbul ignore next -- @preserve */";
/** The hoisted helpers every instrumented file declares (see the module comment). */
export const ENTER = "__cs$E";
export const LEAVE = "__cs$L";

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

/**
 * The value of a constant enum member initializer, as TypeScript computes it: number and string
 * literals, untagged templates without substitutions, earlier members (`B = A`, `E.A`, `E["A"]`),
 * parentheses, unary `+ - ~` and binary `+ - * / % ** << >> >>> & | ^` (`+` concatenates when a
 * side is a string). Anything else (`"c".length`, a call, another enum) is undefined.
 */
function constantValue(
  expr: ts.Expression,
  enumName: string,
  known: Record<string, string | number>,
): string | number | undefined {
  if (ts.isNumericLiteral(expr)) return Number(expr.text);
  if (ts.isStringLiteral(expr) || ts.isNoSubstitutionTemplateLiteral(expr)) return expr.text;
  if (ts.isParenthesizedExpression(expr)) return constantValue(expr.expression, enumName, known);
  if (ts.isIdentifier(expr)) return Object.hasOwn(known, expr.text) ? known[expr.text] : undefined;
  if (ts.isPropertyAccessExpression(expr) && ts.isIdentifier(expr.expression) && expr.expression.text === enumName) {
    return Object.hasOwn(known, expr.name.text) ? known[expr.name.text] : undefined;
  }
  if (
    ts.isElementAccessExpression(expr) &&
    ts.isIdentifier(expr.expression) &&
    expr.expression.text === enumName &&
    ts.isStringLiteralLike(expr.argumentExpression)
  ) {
    const key = expr.argumentExpression.text;
    return Object.hasOwn(known, key) ? known[key] : undefined;
  }
  if (ts.isPrefixUnaryExpression(expr)) {
    const v = constantValue(expr.operand, enumName, known);
    if (typeof v !== "number") return undefined;
    switch (expr.operator) {
      case ts.SyntaxKind.PlusToken:
        return v;
      case ts.SyntaxKind.MinusToken:
        return -v;
      case ts.SyntaxKind.TildeToken:
        return ~v;
    }
    return undefined;
  }
  if (ts.isBinaryExpression(expr)) {
    const a = constantValue(expr.left, enumName, known);
    const b = constantValue(expr.right, enumName, known);
    if (a === undefined || b === undefined) return undefined;
    if (expr.operatorToken.kind === ts.SyntaxKind.PlusToken && (typeof a === "string" || typeof b === "string")) return `${a}${b}`;
    if (typeof a !== "number" || typeof b !== "number") return undefined;
    switch (expr.operatorToken.kind) {
      case ts.SyntaxKind.PlusToken:
        return a + b;
      case ts.SyntaxKind.MinusToken:
        return a - b;
      case ts.SyntaxKind.AsteriskToken:
        return a * b;
      case ts.SyntaxKind.SlashToken:
        return a / b;
      case ts.SyntaxKind.PercentToken:
        return a % b;
      case ts.SyntaxKind.AsteriskAsteriskToken:
        return a ** b;
      case ts.SyntaxKind.LessThanLessThanToken:
        return a << b;
      case ts.SyntaxKind.GreaterThanGreaterThanToken:
        return a >> b;
      case ts.SyntaxKind.GreaterThanGreaterThanGreaterThanToken:
        return a >>> b;
      case ts.SyntaxKind.AmpersandToken:
        return a & b;
      case ts.SyntaxKind.BarToken:
        return a | b;
      case ts.SyntaxKind.CaretToken:
        return a ^ b;
    }
  }
  return undefined;
}

/**
 * Enums declared in `sf`: every member whose value is a constant expression TypeScript can
 * compute ({@link constantValue}) or an auto-incremented number. A computed member (`C = "c".length`)
 * is left out (its examples show the raw value), and so is the auto-increment after it.
 */
export function enumsOf(sf: ts.SourceFile): EnumTable {
  const table: EnumTable = new Map();
  for (const statement of sf.statements) {
    if (!ts.isEnumDeclaration(statement)) continue;
    const enumName = statement.name.text;
    const members: Record<string, string | number> = {};
    let next: number | undefined = 0;
    for (const member of statement.members) {
      const name = ts.isIdentifier(member.name) || ts.isStringLiteral(member.name) ? member.name.text : undefined;
      const init = member.initializer;
      const value: string | number | undefined = init === undefined ? next : constantValue(init, enumName, members);
      next = typeof value === "number" ? value + 1 : undefined;
      if (name !== undefined && value !== undefined && (typeof value === "string" || Number.isFinite(value))) members[name] = value;
    }
    if (Object.keys(members).length) table.set(enumName, { name: enumName, members });
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
    `${ENTER}(${JSON.stringify(fileId)}, ${index}, ${JSON.stringify(region.qualname)}, ` +
    `${region.defLine}, ${JSON.stringify(JSON.stringify(spec))}, ${flagsOf(region)}, [${args}], ${self})`
  );
}

/** Whether `globalThis` is declared anywhere outside a function (so it shadows the global at module level). */
function declaresGlobalThis(sf: ts.SourceFile): boolean {
  if (!sf.text.includes("globalThis")) return false;
  let found = false;
  const named = (name: ts.Node | undefined): boolean => name !== undefined && ts.isIdentifier(name) && name.text === "globalThis";
  const walk = (node: ts.Node): void => {
    if (found) return;
    if (
      (ts.isVariableDeclaration(node) || ts.isBindingElement(node) || ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) ||
        ts.isEnumDeclaration(node) || ts.isModuleDeclaration(node) || ts.isImportSpecifier(node) || ts.isImportClause(node) ||
        ts.isNamespaceImport(node) || ts.isImportEqualsDeclaration(node)) &&
      named((node as { name?: ts.Node }).name)
    ) {
      found = true;
      return;
    }
    if (ts.isFunctionLike(node)) return;
    ts.forEachChild(node, walk);
  };
  ts.forEachChild(sf, walk);
  return found;
}

/** The two helpers, on one line, ignored by Istanbul. */
function prologue(shadowed: boolean): string {
  const g = shadowed ? `(${ENTER}.g ??= (0, eval)("this"))` : "globalThis";
  return (
    `${IGNORE} function ${ENTER}(f, i, q, l, s, k, a, t) { const r = ${g}.__coverset__; ` +
    `return r === undefined || r === null ? -1 : r.enter(f, i, q, l, s, k, a, t); } ` +
    `${IGNORE} function ${LEAVE}(t, v) { const r = ${g}.__coverset__; ` +
    `return r === undefined || r === null ? v : r.leave(t, v); } `
  );
}

/** Where the helpers go: the start of the file, after a hashbang line or a directive prologue. */
function prologuePoint(sf: ts.SourceFile): { at: number; lead: string } {
  let at = 0;
  let lead = "";
  if (sf.text.startsWith("#!")) {
    // A file holding a function has more than its hashbang line.
    const nl = sf.text.search(/\r\n|\r|\n/);
    at = nl === -1 ? sf.text.length : nl + (sf.text.startsWith("\r\n", nl) ? 2 : 1);
  }
  for (const statement of sf.statements) {
    if (!ts.isExpressionStatement(statement) || !ts.isStringLiteral(statement.expression)) break;
    at = statement.end;
    lead = sf.text[statement.end - 1] === ";" ? " " : "; ";
  }
  return { at, lead };
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
      s.prependRight(body.getStart(sf), `${LEAVE}(${call}, (`);
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
        s.prependRight(ret.expression.getStart(sf), `${LEAVE}(${token}, (`);
        s.appendLeft(ret.expression.end, "))");
      } else {
        s.appendLeft(ret.getStart(sf) + "return".length, ` ${LEAVE}(${token}, undefined)`);
      }
    }
    // The `;` ends a last statement written without one on the same line.
    s.appendLeft(body.end - 1, `; ${IGNORE} ${LEAVE}(${token}, undefined); `);
  }
  if (count === 0) return null;
  const { at, lead } = prologuePoint(sf);
  s.appendLeft(at, lead + prologue(declaresGlobalThis(sf)));
  return {
    code: s.toString(),
    map: s.generateMap({ hires: true, source: fileName, includeContent: true }),
    regions: count,
  };
}
