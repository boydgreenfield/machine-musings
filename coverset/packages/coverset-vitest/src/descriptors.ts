/**
 * Type descriptors (`docs/neutral-bundle.md`, "Type descriptors") from declared TypeScript types,
 * through the type checker.
 *
 * `number` → `float`, `bigint` → `int`, `string` → `str`, `boolean` → `bool`,
 * `null`/`undefined`/`void` → `none` (so `T | undefined` and an optional parameter are a union with
 * `none`), string, number and boolean literals (and unions of them, integral numbers included) →
 * `literal`, an `enum` → `enum` with its member names, `T[]`/`Array<T>`/`ReadonlyArray<T>`/
 * `Iterable<T>` → `list`, `[A, B]` → a fixed `tuple`, `Set<T>` → `set`, `Map<K, V>`/`Record<K, V>`/
 * an index signature → `dict`, `Uint8Array`/`Buffer`/`ArrayBuffer` → `bytes`, classes and
 * interfaces → `object` (named `<module>.<Name>` when declared in the repository, as `obj:` shape
 * tokens are), `any`/`unknown`/`never`/type parameters → `any`. An unannotated parameter is `null`.
 * `async` functions describe the awaited return type (the recorder sees the value before the
 * promise wraps it). Descriptors nest at most 30 levels and hold at most 256 arms or values.
 */
import ts from "typescript";

export type Descriptor =
  | { kind: "int" | "float" | "str" | "bool" | "bytes" | "none" | "any" }
  | { kind: "list" | "set" | "frozenset"; elem?: Descriptor }
  | { kind: "tuple"; elems?: Descriptor[]; elem?: Descriptor }
  | { kind: "dict"; key?: Descriptor; value?: Descriptor }
  | { kind: "union"; arms: Descriptor[] }
  | { kind: "literal"; values: (string | number | boolean | null)[] }
  | { kind: "enum"; name: string; members: string[] }
  | { kind: "object"; name: string };

const MAX_DEPTH = 30;
const MAX_ITEMS = 256;
const ANY: Descriptor = { kind: "any" };

export interface DescribeContext {
  checker: ts.TypeChecker;
  /** `module.Name` for a declaration in the repository, else undefined (the bare name is used). */
  qualify: (symbol: ts.Symbol) => string | undefined;
}

function symbolName(symbol: ts.Symbol | undefined): string | undefined {
  return symbol?.getName();
}

function objectName(ctx: DescribeContext, symbol: ts.Symbol | undefined): string {
  if (symbol === undefined) return "Object";
  const name = symbol.getName();
  if (!name || name.startsWith("__")) return "Object";
  return (ctx.qualify(symbol) ?? name).slice(0, 512);
}

function isEnumSymbol(symbol: ts.Symbol | undefined): boolean {
  return symbol !== undefined && (symbol.flags & ts.SymbolFlags.Enum) !== 0;
}

function enumDescriptor(ctx: DescribeContext, symbol: ts.Symbol): Descriptor {
  const members: string[] = [];
  symbol.exports?.forEach((_, key) => {
    members.push(String(key));
  });
  return members.length
    ? { kind: "enum", name: objectName(ctx, symbol), members: members.slice(0, MAX_ITEMS) }
    : { kind: "object", name: objectName(ctx, symbol) };
}

function literalValue(type: ts.Type): string | number | boolean | undefined {
  if (type.isStringLiteral() || type.isNumberLiteral()) return type.value;
  if (type.flags & ts.TypeFlags.BooleanLiteral) return (type as unknown as { intrinsicName: string }).intrinsicName === "true";
  return undefined;
}

function typeArgs(ctx: DescribeContext, type: ts.Type): readonly ts.Type[] {
  if (type.flags & ts.TypeFlags.Object && (type as ts.ObjectType).objectFlags & ts.ObjectFlags.Reference) {
    return ctx.checker.getTypeArguments(type as ts.TypeReference);
  }
  return [];
}

function unionDescriptor(ctx: DescribeContext, type: ts.UnionType, depth: number): Descriptor {
  const arms: Descriptor[] = [];
  const literals: (string | number | boolean)[] = [];
  const bools = new Set<boolean>();
  const enumArms = new Map<ts.Symbol, { values: (string | number)[]; total: number }>();
  for (const arm of type.types) {
    if (arm.flags & ts.TypeFlags.EnumLiteral && !(arm.flags & ts.TypeFlags.Union)) {
      const base = ctx.checker.getBaseTypeOfLiteralType(arm);
      const symbol = base.getSymbol() ?? base.aliasSymbol;
      if (isEnumSymbol(symbol)) {
        const entry = enumArms.get(symbol!) ?? { values: [], total: base.isUnion() ? base.types.length : 1 };
        const value = literalValue(arm);
        if (typeof value === "string" || typeof value === "number") entry.values.push(value);
        enumArms.set(symbol!, entry);
        continue;
      }
    }
    if (arm.flags & ts.TypeFlags.BooleanLiteral) {
      bools.add(literalValue(arm) as boolean);
      continue;
    }
    const value = literalValue(arm);
    if (value !== undefined) {
      literals.push(value);
      continue;
    }
    arms.push(describeType(ctx, arm, depth + 1));
  }
  for (const [symbol, entry] of enumArms) {
    // The whole enum is the enum; a subset of its members is a literal of their values.
    if (entry.values.length >= entry.total) arms.push(enumDescriptor(ctx, symbol));
    else literals.push(...entry.values);
  }
  if (bools.size === 2) arms.push({ kind: "bool" });
  else for (const b of bools) literals.push(b);
  if (literals.length) arms.unshift({ kind: "literal", values: literals.slice(0, MAX_ITEMS) });
  const unique: Descriptor[] = [];
  const seen = new Set<string>();
  for (const arm of arms) {
    const key = JSON.stringify(arm);
    if (!seen.has(key)) {
      seen.add(key);
      unique.push(arm);
    }
  }
  if (unique.length === 1) return unique[0]!;
  return { kind: "union", arms: unique.slice(0, MAX_ITEMS) };
}

/** The descriptor of a checker type. */
export function describeType(ctx: DescribeContext, type: ts.Type, depth = 0): Descriptor {
  if (depth > MAX_DEPTH) return ANY;
  const { checker } = ctx;
  const flags = type.flags;
  if (flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown | ts.TypeFlags.Never | ts.TypeFlags.TypeParameter)) return ANY;
  if (flags & (ts.TypeFlags.Null | ts.TypeFlags.Undefined | ts.TypeFlags.Void)) return { kind: "none" };
  if (flags & ts.TypeFlags.Boolean) return { kind: "bool" };
  const symbol = type.getSymbol() ?? type.aliasSymbol;
  if (flags & ts.TypeFlags.EnumLiteral && flags & ts.TypeFlags.Union && isEnumSymbol(symbol)) {
    return enumDescriptor(ctx, symbol!);
  }
  if (type.isUnion()) return unionDescriptor(ctx, type, depth);
  const literal = literalValue(type);
  if (literal !== undefined) {
    if (flags & ts.TypeFlags.EnumLiteral) {
      const base = checker.getBaseTypeOfLiteralType(type);
      const baseSymbol = base.getSymbol();
      if (isEnumSymbol(baseSymbol) && !base.isUnion()) return enumDescriptor(ctx, baseSymbol!);
    }
    return { kind: "literal", values: [literal] };
  }
  if (flags & (ts.TypeFlags.BigInt | ts.TypeFlags.BigIntLiteral)) return { kind: "int" };
  if (flags & ts.TypeFlags.NumberLike) return { kind: "float" };
  if (flags & ts.TypeFlags.StringLike) return { kind: "str" };
  if (flags & ts.TypeFlags.ESSymbolLike) return { kind: "object", name: "Symbol" };
  if (type.isIntersection()) {
    // A branded primitive (`number & { __brand: "Cents" }`) is its primitive.
    for (const part of type.types) {
      if (part.flags & ts.TypeFlags.NumberLike) return { kind: "float" };
      if (part.flags & ts.TypeFlags.StringLike) return { kind: "str" };
      if (part.flags & (ts.TypeFlags.BigInt | ts.TypeFlags.BigIntLiteral)) return { kind: "int" };
    }
    return ANY;
  }
  if (!(flags & ts.TypeFlags.Object)) return ANY;
  if (checker.isTupleType(type)) {
    const target = (type as ts.TupleTypeReference).target;
    const elements = checker.getTypeArguments(type as ts.TypeReference);
    const rest = target.elementFlags.findIndex((f) => f & (ts.ElementFlags.Rest | ts.ElementFlags.Variadic));
    if (rest >= 0) return { kind: "tuple", elem: describeType(ctx, elements[rest]!, depth + 1) };
    return { kind: "tuple", elems: elements.slice(0, MAX_ITEMS).map((t) => describeType(ctx, t, depth + 1)) };
  }
  if (checker.isArrayType(type)) {
    const [elem] = typeArgs(ctx, type);
    return elem === undefined ? { kind: "list" } : { kind: "list", elem: describeType(ctx, elem, depth + 1) };
  }
  if (type.aliasSymbol?.getName() === "Record" && type.aliasTypeArguments?.length === 2) {
    const [key, value] = type.aliasTypeArguments;
    return { kind: "dict", key: describeType(ctx, key!, depth + 1), value: describeType(ctx, value!, depth + 1) };
  }
  if (isEnumSymbol(symbol)) return enumDescriptor(ctx, symbol!);
  const name = symbolName(type.getSymbol());
  const args = typeArgs(ctx, type);
  const arg = (i: number): Descriptor | undefined => (args[i] === undefined ? undefined : describeType(ctx, args[i]!, depth + 1));
  switch (name) {
    case "Array":
    case "ReadonlyArray":
    case "Iterable":
    case "IterableIterator":
    case "ArrayLike": {
      const elem = arg(0);
      return elem === undefined ? { kind: "list" } : { kind: "list", elem };
    }
    case "Set":
    case "ReadonlySet": {
      const elem = arg(0);
      return elem === undefined ? { kind: "set" } : { kind: "set", elem };
    }
    case "Map":
    case "ReadonlyMap": {
      const key = arg(0);
      const value = arg(1);
      return { kind: "dict", ...(key ? { key } : {}), ...(value ? { value } : {}) };
    }
    case "Uint8Array":
    case "Buffer":
    case "ArrayBuffer":
      return { kind: "bytes" };
  }
  const props = type.getProperties();
  const stringIndex = checker.getIndexInfoOfType(type, ts.IndexKind.String);
  if (stringIndex !== undefined && props.length === 0) {
    return { kind: "dict", key: { kind: "str" }, value: describeType(ctx, stringIndex.type, depth + 1) };
  }
  if (type.getCallSignatures().length > 0 && props.length === 0) return { kind: "object", name: "Function" };
  return { kind: "object", name: objectName(ctx, type.getSymbol() ?? type.aliasSymbol) };
}

function withNone(d: Descriptor): Descriptor {
  if (d.kind === "none" || d.kind === "any") return d;
  if (d.kind === "union") {
    return d.arms.some((a) => a.kind === "none") ? d : { kind: "union", arms: [...d.arms, { kind: "none" }] };
  }
  return { kind: "union", arms: [d, { kind: "none" }] };
}

/** A parameter's declared descriptor (`null` when it has no annotation). */
export function paramDescriptor(ctx: DescribeContext, param: ts.ParameterDeclaration): Descriptor | null {
  if (param.type === undefined) return null;
  const d = describeType(ctx, ctx.checker.getTypeFromTypeNode(param.type));
  return param.questionToken !== undefined ? withNone(d) : d;
}

/** The declared return type (awaited for `async` functions), or null without an annotation. */
export function returnDescriptor(ctx: DescribeContext, fn: ts.FunctionLikeDeclaration, isAsync: boolean): Descriptor | null {
  if (fn.type === undefined) return null;
  let type = ctx.checker.getTypeFromTypeNode(fn.type);
  if (isAsync) type = ctx.checker.getAwaitedType(type) ?? type;
  return describeType(ctx, type);
}
