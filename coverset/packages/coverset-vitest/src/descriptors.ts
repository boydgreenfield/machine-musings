/**
 * Type descriptors (`docs/neutral-bundle.md`, "Type descriptors") from declared TypeScript types,
 * through the type checker.
 *
 * `number` → `float`, `bigint` → `int`, `string` → `str`, `boolean` → `bool`,
 * `null`/`undefined`/`void` → `none` (so `T | undefined` and an optional parameter are a union with
 * `none`), string, number and boolean literals (and unions of them, integral numbers included) →
 * `literal`, an `enum` → `enum` with its member names, `T[]`/`Array<T>`/`ReadonlyArray<T>`/
 * `Iterable<T>` → `list`, `[A, B]` → a fixed `tuple`, `Set<T>` → `set`, `Map<K, V>`/`Record<K, V>`/
 * an index signature → `dict`, `Uint8Array`/`Buffer`/`ArrayBuffer` → `bytes`, a class (or a
 * library class declared as an interface and a constructor variable, such as `Date`) → `object`
 * (named `<module>.<Name>` when declared in the repository, as `obj:` shape tokens are), a type
 * with call or construct signatures (a function type, `typeof SomeClass`) → `object` `Function`,
 * any other object type (an interface, a type literal, a mapped type, an alias of one) → `dict`
 * with `str` keys (what a plain object observes as; the descriptor has no field for its name),
 * `any`/`unknown`/`never`/type parameters → `any`. An unannotated parameter is `null`.
 * `async` functions describe the awaited return type (the recorder sees the value before the
 * promise wraps it). Union arms and literal values keep the order they are written in (through
 * type aliases), the literal arm standing where its first value is. Descriptors nest at most 30
 * levels and hold at most 256 arms or values.
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

/** A class, or a library class written as an interface merged with a constructor variable (`Date`). */
function isClassSymbol(symbol: ts.Symbol | undefined): boolean {
  if (symbol === undefined) return false;
  if (symbol.flags & ts.SymbolFlags.Class) return true;
  return (symbol.flags & ts.SymbolFlags.Interface) !== 0 && (symbol.flags & ts.SymbolFlags.Variable) !== 0;
}

/** What a plain object observes as (`dict|...|key:str`). */
function plainObject(ctx: DescribeContext, type: ts.Type, depth: number): Descriptor {
  const index = ctx.checker.getIndexInfoOfType(type, ts.IndexKind.String);
  return index === undefined
    ? { kind: "dict", key: { kind: "str" } }
    : { kind: "dict", key: { kind: "str" }, value: describeType(ctx, index.type, depth + 1) };
}

/**
 * The constituents a union type node lists, in the order they are written: through parentheses,
 * nested unions and (non-generic) type aliases; anything else is the checker's type of the node
 * (an enum's members in declaration order).
 */
export function declaredOrder(checker: ts.TypeChecker, node: ts.TypeNode | undefined, depth = 0): ts.Type[] {
  if (node === undefined || depth > 16) return [];
  if (ts.isParenthesizedTypeNode(node)) return declaredOrder(checker, node.type, depth + 1);
  if (ts.isUnionTypeNode(node)) return node.types.flatMap((t) => declaredOrder(checker, t, depth + 1));
  if (ts.isTypeReferenceNode(node) && node.typeArguments === undefined) {
    let symbol = checker.getSymbolAtLocation(node.typeName);
    if (symbol !== undefined && symbol.flags & ts.SymbolFlags.Alias) symbol = checker.getAliasedSymbol(symbol);
    const alias = symbol?.declarations?.find(ts.isTypeAliasDeclaration);
    if (alias !== undefined && alias.typeParameters === undefined) return declaredOrder(checker, alias.type, depth + 1);
  }
  const type = checker.getTypeFromTypeNode(node);
  return type.isUnion() ? [...type.types] : [type];
}

/** `types` sorted by their first position in `order` (unlisted ones last, in their own order). */
function inOrder(types: readonly ts.Type[], order: readonly ts.Type[] | undefined): readonly ts.Type[] {
  if (order === undefined || order.length === 0) return types;
  const rank = new Map<ts.Type, number>();
  order.forEach((t, i) => {
    if (!rank.has(t)) rank.set(t, i);
  });
  return types
    .map((t, i) => ({ t, r: rank.get(t) ?? order.length + i }))
    .sort((a, b) => a.r - b.r)
    .map((x) => x.t);
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

function unionDescriptor(ctx: DescribeContext, type: ts.UnionType, depth: number, order?: readonly ts.Type[]): Descriptor {
  type Item =
    | { cat: "enum"; symbol: ts.Symbol; value: string | number | undefined; total: number }
    | { cat: "bool"; value: boolean }
    | { cat: "lit"; value: string | number | boolean }
    | { cat: "desc"; arm: ts.Type };
  const items: Item[] = [];
  for (const arm of inOrder(type.types, order)) {
    if (arm.flags & ts.TypeFlags.EnumLiteral && !(arm.flags & ts.TypeFlags.Union)) {
      const base = ctx.checker.getBaseTypeOfLiteralType(arm);
      const symbol = base.getSymbol() ?? base.aliasSymbol;
      if (isEnumSymbol(symbol)) {
        const value = literalValue(arm);
        items.push({
          cat: "enum",
          symbol: symbol!,
          value: typeof value === "string" || typeof value === "number" ? value : undefined,
          total: base.isUnion() ? base.types.length : 1,
        });
        continue;
      }
    }
    if (arm.flags & ts.TypeFlags.BooleanLiteral) {
      items.push({ cat: "bool", value: literalValue(arm) as boolean });
      continue;
    }
    const value = literalValue(arm);
    if (value !== undefined) items.push({ cat: "lit", value });
    else items.push({ cat: "desc", arm });
  }
  const enumCounts = new Map<ts.Symbol, number>();
  for (const item of items) if (item.cat === "enum") enumCounts.set(item.symbol, (enumCounts.get(item.symbol) ?? 0) + 1);
  const bothBools = new Set(items.filter((i) => i.cat === "bool").map((i) => i.value)).size === 2;
  // Arms in written order; the literal arm stands where its first value is.
  const LITERAL = Symbol("literal");
  const slots: (Descriptor | typeof LITERAL)[] = [];
  const literals: (string | number | boolean)[] = [];
  const done = new Set<unknown>();
  const literal = (value: string | number | boolean | undefined): void => {
    if (value === undefined) return;
    if (!done.has(LITERAL)) {
      done.add(LITERAL);
      slots.push(LITERAL);
    }
    literals.push(value);
  };
  for (const item of items) {
    if (item.cat === "enum") {
      // The whole enum is the enum; a subset of its members is a literal of their values.
      if ((enumCounts.get(item.symbol) ?? 0) >= item.total) {
        if (!done.has(item.symbol)) {
          done.add(item.symbol);
          slots.push(enumDescriptor(ctx, item.symbol));
        }
      } else {
        literal(item.value);
      }
    } else if (item.cat === "bool") {
      if (!bothBools) literal(item.value);
      else if (!done.has("bool")) {
        done.add("bool");
        slots.push({ kind: "bool" });
      }
    } else if (item.cat === "lit") {
      literal(item.value);
    } else {
      slots.push(describeType(ctx, item.arm, depth + 1));
    }
  }
  const unique: Descriptor[] = [];
  const seen = new Set<string>();
  for (const slot of slots) {
    const arm: Descriptor = slot === LITERAL ? { kind: "literal", values: literals.slice(0, MAX_ITEMS) } : slot;
    const key = JSON.stringify(arm);
    if (!seen.has(key)) {
      seen.add(key);
      unique.push(arm);
    }
  }
  if (unique.length === 1) return unique[0]!;
  return { kind: "union", arms: unique.slice(0, MAX_ITEMS) };
}

/**
 * The descriptor of a checker type; `order` (from {@link declaredOrder}) puts the arms of a union
 * in written order.
 */
export function describeType(ctx: DescribeContext, type: ts.Type, depth = 0, order?: readonly ts.Type[]): Descriptor {
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
  if (type.isUnion()) return unionDescriptor(ctx, type, depth, order);
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
  // Functions and constructors (`typeof SomeClass`) observe as obj:Function.
  if (type.getCallSignatures().length > 0 || type.getConstructSignatures().length > 0) return { kind: "object", name: "Function" };
  const objectSymbol = type.getSymbol();
  if (isClassSymbol(objectSymbol)) return { kind: "object", name: objectName(ctx, objectSymbol) };
  // An interface, a type literal, a mapped type or an alias of one: a plain object at runtime.
  return plainObject(ctx, type, depth);
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
  const d = describeType(ctx, ctx.checker.getTypeFromTypeNode(param.type), 0, declaredOrder(ctx.checker, param.type));
  return param.questionToken !== undefined ? withNone(d) : d;
}

/** The declared return type (awaited for `async` functions), or null without an annotation. */
export function returnDescriptor(ctx: DescribeContext, fn: ts.FunctionLikeDeclaration, isAsync: boolean): Descriptor | null {
  if (fn.type === undefined) return null;
  let type = ctx.checker.getTypeFromTypeNode(fn.type);
  let node: ts.TypeNode | undefined = fn.type;
  if (isAsync) {
    type = ctx.checker.getAwaitedType(type) ?? type;
    node = ts.isTypeReferenceNode(node) && node.typeArguments?.length === 1 ? node.typeArguments[0] : undefined;
  }
  return describeType(ctx, type, 0, declaredOrder(ctx.checker, node));
}
