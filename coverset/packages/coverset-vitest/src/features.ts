/**
 * Shape keys of JavaScript values: a port of `pytest_coverset/features.py` onto the value
 * vocabulary of `docs/neutral-bundle.md` ("Shape keys").
 *
 * A key is `<type>` followed by `|`-joined feature tokens sorted alphabetically. JavaScript values
 * map onto the vocabulary as the specification says: integral numbers are `int`, other numbers
 * `float` (`NaN` is `float|nan`, the infinities `float|inf` and `float|inf|inf-neg`), strings
 * `str`, booleans `bool`, `null` and `undefined` `NoneType`, arrays `list`, plain objects and
 * `Map` `dict`, `Set` `set`, `Uint8Array`/`Buffer`/`ArrayBuffer` `bytes`, and anything else
 * `obj:<module>.<ClassName>` (the module is the defining file's repository path without its
 * extension, dotted, when the recorder has seen the class's methods run; else the bare name).
 *
 * Safety: no user code ever runs. Only `typeof`, `Array.isArray`, `Object.getPrototypeOf`,
 * `Object.getOwnPropertyDescriptor` (data properties only: an accessor's value is never read),
 * `Object.keys` (own enumerable keys) and the intrinsic iterators of exact `Map` and `Set`
 * instances are used; a `Proxy` is never looked into: a proxy value, an object whose prototype is
 * a proxy and a proxied constructor are `obj:Proxy`, checked with `util.types.isProxy` before any
 * read that could reach a trap. Containers are summarised over at most {@link MAX_ELEMENTS}
 * leading elements; whitespace and digit features of long strings over a {@link SCAN_CHARS}
 * code-point prefix, and strings longer than {@link MAX_KIND_CHARS} get no `kind:`.
 *
 * `obj:` names are sanitised as the grammar requires (whitespace, `|` and characters Python does
 * not print become `_`) and cut at {@link MAX_NAME_CHARS}, so even `dict|elem:obj:…|key:obj:…`
 * stays within the 512-character key limit.
 */
import { types as utilTypes } from "node:util";

export const MAX_ELEMENTS = 32;
export const MAX_KIND_CHARS = 2048;
export const SCAN_CHARS = 4096;
export const BIG_INT = 2 ** 31;
/** Longest `obj:` name: two of them plus the other features of a key stay under 512 characters. */
export const MAX_NAME_CHARS = 200;
/** Bytes compared when ordering lists of byte strings (a longer common prefix counts as equal). */
export const SCAN_BYTES = 4096;

/** Python's `str.isspace()` set (also what `\s` matches in a Python `str` pattern). */
const WS = "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
/** Python's `\d` in a `str` pattern: any Unicode decimal digit. */
const D = "\\p{Nd}";

const TWO_LEVEL_SUFFIXES = new Set([
  "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
  "com.au", "net.au", "org.au", "edu.au", "gov.au",
  "co.jp", "ne.jp", "or.jp", "ac.jp",
  "co.nz", "org.nz", "co.za", "co.in", "co.id", "co.il", "co.kr",
  "com.br", "com.mx", "com.ar", "com.sg", "com.hk", "com.tw", "com.tr", "com.pl",
  "com.my", "com.ph", "com.vn", "com.eg", "com.pk", "com.ng", "com.cn", "com.co",
]);

const KNOWN_UNITS = new Set([
  "ns", "us", "µs", "ms", "s", "sec", "secs", "second", "seconds",
  "m", "min", "mins", "minute", "minutes", "h", "hr", "hrs", "hour", "hours",
  "d", "day", "days", "w", "wk", "wks", "week", "weeks", "y", "yr", "yrs", "year", "years",
  "b", "kb", "mb", "gb", "tb", "kib", "mib", "gib", "tib", "k",
  "mm", "cm", "km", "in", "ft", "mi", "g", "kg", "mg", "lb", "lbs", "oz",
  "px", "pt", "em", "rem",
]);

const EMAIL_RE = new RegExp(`^([^@${WS}]+)@([^@${WS}]+\\.[^@${WS}]+)$`, "u");
const QUANTITY_RE = new RegExp(`^[${WS}]*[-+]?[${D}.]+[${WS}]*[A-Za-z]+`, "u");
const QUANTITY_HEAD_RE = new RegExp(`[${WS}]*([-+]?)`, "uy");
const QUANTITY_SEGMENT_RE = new RegExp(
  `[${WS}]*(${D}+(?:\\.${D}*)?|\\.${D}+)[${WS}]*([\\p{L}\\p{Nl}\\p{No}]+)`,
  "uy",
);
const NUMERIC_RE = new RegExp(`^[${WS}]*[-+]?(?:${D}+(?:\\.${D}*)?|\\.${D}+)(?:[eE][-+]?${D}+)?[${WS}]*$`, "u");
const ISO_DATE_RE = new RegExp(
  `^${D}{4}-${D}{2}-${D}{2}(?:[T ]${D}{2}:${D}{2}(?::${D}{2}(?:\\.${D}+)?)?(?:Z|[+-]${D}{2}:?${D}{2})?)?$`,
  "u",
);
const URL_RE = new RegExp(`^[A-Za-z][A-Za-z0-9+.-]*://[^${WS}]+$`, "u");
const DIGIT_RE = new RegExp(D, "u");
const ALL_WS_RE = new RegExp(`^[${WS}]+$`, "u");
const WS_CHAR_RE = new RegExp(`^[${WS}]$`, "u");
const NON_WS_RE = new RegExp(`[^${WS}]`, "u");

const ARRAY_PROTO = Array.prototype;
const OBJECT_PROTO = Object.prototype;
const MAP_PROTO = Map.prototype;
const SET_PROTO = Set.prototype;
const MAP_ENTRIES = Map.prototype.entries;
const SET_VALUES = Set.prototype.values;
const MAP_SIZE = Object.getOwnPropertyDescriptor(Map.prototype, "size")!.get!;
const SET_SIZE = Object.getOwnPropertyDescriptor(Set.prototype, "size")!.get!;
const TYPED_LENGTH = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(Uint8Array.prototype), "length")!.get!;
const ARRAY_BUFFER_LENGTH = Object.getOwnPropertyDescriptor(ArrayBuffer.prototype, "byteLength")!.get!;

/** Resolves a constructor to its `obj:` token name (`module.Class`), when the recorder knows it. */
export type ClassNamer = (ctor: Function) => string | undefined;

let classNamer: ClassNamer = () => undefined;

/** Install the resolver the recorder fills as it sees methods of measured classes run. */
export function setClassNamer(namer: ClassNamer): void {
  classNamer = namer;
}

export function lenBucket(n: number): string {
  if (n <= 1) return String(n);
  if (n <= 10) return "2-10";
  if (n <= 100) return "11-100";
  if (n <= 1000) return "101-1000";
  return ">1000";
}

function join(head: string, features: string[]): string {
  return features.length ? [head, ...features.sort()].join("|") : head;
}

/** A data property's value, or `undefined` with `ok: false` for an accessor (never invoked). */
function ownData(obj: object, key: PropertyKey): { ok: boolean; value: unknown } {
  const desc = Object.getOwnPropertyDescriptor(obj, key);
  if (desc === undefined) return { ok: true, value: undefined }; // an array hole reads as undefined
  if (!("value" in desc)) return { ok: false, value: undefined };
  return { ok: true, value: desc.value };
}

/**
 * A name usable after `obj:`: whitespace, `|` and every character Python's `str.isprintable()`
 * rejects (Unicode categories C and Z: controls, format characters such as U+200D, separators)
 * become `_`, and the result is cut at {@link MAX_NAME_CHARS} code points.
 */
export function safeName(text: string): string {
  const cleaned = text.replace(/[\s|\p{C}\p{Z}]+/gu, "_");
  if (!cleaned.length) return "<anonymous>";
  return cleaned.length <= MAX_NAME_CHARS ? cleaned : codePointPrefix(cleaned, MAX_NAME_CHARS);
}

/** The token of a proxied prototype or constructor (never looked into). */
export const PROXY_TOKEN = "obj:Proxy";

/**
 * The constructor whose prototype `proto` is (read as a data property), or undefined; never
 * reads a proxy (callers check {@link utilTypes.isProxy} on `proto` to name it `obj:Proxy`).
 */
export function constructorOf(proto: object | null): Function | undefined {
  if (proto === null || utilTypes.isProxy(proto)) return undefined;
  const ctor = ownData(proto, "constructor");
  return ctor.ok && typeof ctor.value === "function" ? (ctor.value as Function) : undefined;
}

/** `obj:` name of a constructor: the recorder's `module.Class`, else the function's own name. */
export function classToken(ctor: Function | undefined): string {
  if (ctor === undefined) return "obj:Object";
  if (utilTypes.isProxy(ctor)) return PROXY_TOKEN;
  const known = classNamer(ctor);
  if (known !== undefined) return "obj:" + safeName(known);
  const name = ownData(ctor, "name");
  return "obj:" + safeName(typeof name.value === "string" ? name.value : "");
}

/** `obj:` token of an object that is not a builtin container: its class, or `obj:Proxy`. */
export function instanceToken(obj: object): string {
  if (utilTypes.isProxy(obj)) return PROXY_TOKEN;
  const proto = Object.getPrototypeOf(obj) as object | null;
  if (proto !== null && utilTypes.isProxy(proto)) return PROXY_TOKEN;
  return classToken(constructorOf(proto));
}

type Kind =
  | "none" | "bool" | "int" | "float" | "str" | "bytes" | "list" | "dict" | "map" | "set" | "obj";

interface Classified {
  kind: Kind;
  token: string;
}

/** The type token of a value without computing its features. */
function classify(value: unknown): Classified {
  switch (typeof value) {
    case "undefined":
      return { kind: "none", token: "NoneType" };
    case "boolean":
      return { kind: "bool", token: "bool" };
    case "number":
      return Number.isInteger(value) ? { kind: "int", token: "int" } : { kind: "float", token: "float" };
    case "bigint":
      return { kind: "int", token: "int" };
    case "string":
      return { kind: "str", token: "str" };
    case "symbol":
      return { kind: "obj", token: "obj:Symbol" };
    case "function":
      return { kind: "obj", token: "obj:Function" };
  }
  if (value === null) return { kind: "none", token: "NoneType" };
  const obj = value as object;
  if (utilTypes.isProxy(obj)) return { kind: "obj", token: PROXY_TOKEN };
  if (utilTypes.isUint8Array(obj) || utilTypes.isArrayBuffer(obj)) return { kind: "bytes", token: "bytes" };
  const proto = Object.getPrototypeOf(obj) as object | null;
  if (Array.isArray(obj) && proto === ARRAY_PROTO) return { kind: "list", token: "list" };
  if (proto === OBJECT_PROTO || proto === null) return { kind: "dict", token: "dict" };
  if (proto === MAP_PROTO && utilTypes.isMap(obj)) return { kind: "map", token: "dict" };
  if (proto === SET_PROTO && utilTypes.isSet(obj)) return { kind: "set", token: "set" };
  if (proto !== null && utilTypes.isProxy(proto)) return { kind: "obj", token: PROXY_TOKEN };
  return { kind: "obj", token: classToken(constructorOf(proto)) };
}

/** The type token of `value` (`int`, `str`, `list`, `obj:src.cart.Cart`, ...). */
export function typeToken(value: unknown): string {
  return classify(value).token;
}

/** The canonical shape key of `value`; never runs user code. */
export function shapeKey(value: unknown): string {
  const c = classify(value);
  switch (c.kind) {
    case "none":
      return "NoneType";
    case "bool":
      return value ? "bool|true" : "bool|false";
    case "int":
      return join("int", intFeatures(value as number | bigint));
    case "float":
      return join("float", floatFeatures(value as number));
    case "str":
      return join("str", strFeatures(value as string));
    case "bytes": {
      const n = utilTypes.isArrayBuffer(value) ? ARRAY_BUFFER_LENGTH.call(value) : TYPED_LENGTH.call(value);
      return join("bytes", ["len:" + lenBucket(n as number)]);
    }
    case "list": {
      const arr = value as unknown[];
      const n = arr.length;
      const sample: unknown[] = [];
      for (let i = 0; i < Math.min(n, MAX_ELEMENTS); i++) {
        const item = ownData(arr, i);
        sample.push(item.ok ? item.value : HOLE);
      }
      return join("list", sequenceFeatures(n, sample));
    }
    case "set": {
      const n = SET_SIZE.call(value) as number;
      const sample: unknown[] = [];
      const it = SET_VALUES.call(value as Set<unknown>);
      for (let i = 0; i < MAX_ELEMENTS; i++) {
        const step = it.next();
        if (step.done) break;
        sample.push(step.value);
      }
      return join("set", collectionFeatures(n, sample));
    }
    case "map": {
      const n = MAP_SIZE.call(value) as number;
      const keys: unknown[] = [];
      const values: unknown[] = [];
      const it = MAP_ENTRIES.call(value as Map<unknown, unknown>);
      for (let i = 0; i < MAX_ELEMENTS; i++) {
        const step = it.next();
        if (step.done) break;
        keys.push(step.value[0]);
        values.push(step.value[1]);
      }
      return join("dict", dictFeatures(n, keys, values));
    }
    case "dict": {
      const allKeys = Object.keys(value as object);
      const keys = allKeys.slice(0, MAX_ELEMENTS);
      const values = keys.map((k) => {
        const item = ownData(value as object, k);
        return item.ok ? item.value : HOLE;
      });
      return join("dict", dictFeatures(allKeys.length, keys, values));
    }
    default:
      return c.token;
  }
}

/** Stands for an element that is an accessor (never read): its type token is `obj:accessor`. */
const HOLE: unique symbol = Symbol("coverset.hole");

function intFeatures(value: number | bigint): string[] {
  const sign = value < 0 ? "neg" : value == 0 ? "zero" : "pos";
  const big = typeof value === "bigint" ? value > BigInt(BIG_INT) || value < -BigInt(BIG_INT) : Math.abs(value) > BIG_INT;
  return big ? [sign, "big"] : [sign];
}

function floatFeatures(value: number): string[] {
  if (Number.isNaN(value)) return ["nan"];
  if (!Number.isFinite(value)) return value < 0 ? ["inf", "inf-neg"] : ["inf"];
  // Integral numbers are `int`, so `integral` never appears on a JavaScript float.
  return [value < 0 ? "neg" : value === 0 ? "zero" : "pos"];
}

/** Length in code points (Python's `len`), counted without building an array. */
export function codePointLength(text: string): number {
  let n = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) {
      const d = text.charCodeAt(i + 1);
      if (d >= 0xdc00 && d <= 0xdfff) i++;
    }
    n++;
  }
  return n;
}

/** The first `n` code points of `text` (a surrogate pair is never split). */
export function codePointPrefix(text: string, n: number): string {
  if (text.length <= n) return text;
  let units = 0;
  for (let i = 0; i < n && units < text.length; i++) {
    const c = text.charCodeAt(units);
    units += c >= 0xd800 && c <= 0xdbff && units + 1 < text.length && (text.charCodeAt(units + 1) & 0xfc00) === 0xdc00 ? 2 : 1;
  }
  return text.slice(0, units);
}

/** Python's `str` comparison: by code point (JavaScript's `<` compares UTF-16 units). */
export function compareCodePoints(a: string, b: string): number {
  if (a === b) return 0;
  let i = 0;
  while (i < a.length && i < b.length) {
    const x = a.codePointAt(i)!;
    const y = b.codePointAt(i)!;
    if (x !== y) return x - y;
    i += x > 0xffff ? 2 : 1;
  }
  return (i < a.length ? 1 : 0) - (i < b.length ? 1 : 0);
}

function isAscii(text: string): boolean {
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) > 0x7f) return false;
  return true;
}

function strFeatures(value: string): string[] {
  const n = codePointLength(value);
  const feats = ["len:" + lenBucket(n)];
  if (n === 0) {
    feats.push("empty");
    return feats;
  }
  const head = value.length <= SCAN_CHARS ? value : codePointPrefix(value, SCAN_CHARS);
  if (ALL_WS_RE.test(head)) {
    feats.push("whitespace");
    return feats;
  }
  feats.push(isAscii(value) ? "ascii" : "unicode");
  if (WS_CHAR_RE.test(value[0]!) || WS_CHAR_RE.test(value[value.length - 1]!)) feats.push("ws-edge");
  if (DIGIT_RE.test(head)) feats.push("digits");
  if (n <= MAX_KIND_CHARS) {
    const kind = stringKind(value);
    if (kind !== null) feats.push("kind:" + kind);
  }
  return feats;
}

/** `email/<sub>`, `numeric`, `quantity/<sub>`, `iso-date` or `url` (first match), else null. */
export function stringKind(value: string): string | null {
  const email = EMAIL_RE.exec(value);
  if (email !== null && email[2]!.split(".").every((label) => label.length > 0)) {
    return "email/" + emailSubclass(email[1]!, email[2]!);
  }
  if (NUMERIC_RE.test(value)) return "numeric";
  if (QUANTITY_RE.test(value)) return "quantity/" + quantitySubclass(value);
  if (ISO_DATE_RE.test(value)) return "iso-date";
  if (URL_RE.test(value)) return "url";
  return null;
}

/** `idn` > `multi-label-domain` > `plus-tag` > `uppercase` > `plain` (first match wins). */
export function emailSubclass(local: string, domain: string): string {
  const labels = domain.toLowerCase().split(".");
  if (!(isAscii(local) && isAscii(domain)) || labels.some((label) => label.startsWith("xn--"))) return "idn";
  const nLabels =
    labels.length >= 3 && TWO_LEVEL_SUFFIXES.has(labels.slice(-2).join(".")) ? labels.length - 1 : labels.length;
  if (nLabels >= 3) return "multi-label-domain";
  if (local.includes("+")) return "plus-tag";
  if (local !== local.toLowerCase() || domain !== domain.toLowerCase()) return "uppercase";
  return "plain";
}

/** `trailing-garbage` > `signed` > `multi-unit` > `decimal` > `multi-letter-unit` > `single-unit`. */
export function quantitySubclass(value: string): string {
  QUANTITY_HEAD_RE.lastIndex = 0;
  const head = QUANTITY_HEAD_RE.exec(value);
  let pos = head !== null ? QUANTITY_HEAD_RE.lastIndex : 0;
  const signed = head !== null && head[1] !== "";
  const units: string[] = [];
  let decimal = false;
  for (;;) {
    QUANTITY_SEGMENT_RE.lastIndex = pos;
    const segment = QUANTITY_SEGMENT_RE.exec(value);
    if (segment === null) break;
    units.push(segment[2]!);
    decimal = decimal || segment[1]!.includes(".");
    pos = QUANTITY_SEGMENT_RE.lastIndex;
  }
  if (NON_WS_RE.test(value.slice(pos)) || units.length === 0 || units.some(garbageUnit)) return "trailing-garbage";
  if (signed) return "signed";
  if (units.length > 1) return "multi-unit";
  if (decimal) return "decimal";
  return codePointLength(units[0]!) > 1 ? "multi-letter-unit" : "single-unit";
}

function garbageUnit(unit: string): boolean {
  const lowered = unit.toLowerCase();
  if (KNOWN_UNITS.has(lowered)) return false;
  for (const known of KNOWN_UNITS) if (lowered.startsWith(known)) return true;
  return false;
}

function elemToken(sample: unknown[]): string | null {
  if (sample.length === 0) return null;
  const first = elementType(sample[0]);
  for (const item of sample) if (elementType(item) !== first) return "elem:mixed";
  return "elem:" + first;
}

function elementType(item: unknown): string {
  return item === HOLE ? "obj:accessor" : typeToken(item);
}

function isNumber(x: unknown): x is number | bigint {
  return typeof x === "number" || typeof x === "bigint";
}

const LONG_MAX = 2n ** 63n - 1n;
const LONG_MIN = -(2n ** 63n);
const SMALL_INT = 2 ** 47;

/** `a + b` with Python's int/float semantics (`bigint` is an int, `number` a float); null for OverflowError. */
function pyAdd(a: bigint | number, b: bigint | number): bigint | number | null {
  if (typeof a === "bigint" && typeof b === "bigint") return a + b;
  const fa = typeof a === "bigint" ? Number(a) : a;
  const fb = typeof b === "bigint" ? Number(b) : b;
  if ((typeof a === "bigint" && !Number.isFinite(fa)) || (typeof b === "bigint" && !Number.isFinite(fb))) return null;
  return fa + fb;
}

/**
 * CPython 3.12+'s `sum()` of the Python values these numbers stand for (integral numbers and
 * bigints are ints, other numbers floats), step for step, or null where Python raises
 * `OverflowError` (an int too large for a float next to a float):
 *
 * 1. while the items are ints and the running total fits a C `long`, the total is exact;
 * 2. the first float `x` makes it `float(total) + x`, and from then on floats are added with
 *    Neumaier's compensation while ints that fit a `long` are added as plain floats
 *    (uncompensated); the compensation is folded in at the end, or before 3. when it is finite;
 * 3. an int beyond a `long` (in 1. or 2.) switches to plain `+` for the rest: exact between ints,
 *    rounded as soon as a float is involved.
 *
 * Checked against CPython 3.13's `sum` on random lists (`test/features.test.ts`).
 */
export function pythonSum(values: readonly (number | bigint)[]): bigint | number | null {
  if (values.length <= 64 && values.every((x) => typeof x === "number" && Number.isInteger(x) && Math.abs(x) <= SMALL_INT)) {
    // At most 64 integers of at most 2**47: the double sum is exact, and so is Python's.
    return (values as number[]).reduce((a, b) => a + b, 0);
  }
  const xs = values.map((v) => (typeof v === "bigint" ? v : Number.isInteger(v) ? BigInt(v) : v));
  let k = 0;
  let exact = 0n;
  let result: bigint | number | null | undefined;
  for (; k < xs.length; k++) {
    const x = xs[k]!;
    if (typeof x === "bigint" && x >= LONG_MIN && x <= LONG_MAX && exact + x >= LONG_MIN && exact + x <= LONG_MAX) {
      exact += x;
      continue;
    }
    result = pyAdd(exact, x);
    k++;
    break;
  }
  if (result === undefined) return exact;
  if (result === null) return null;
  if (typeof result === "number") {
    let total = result;
    let compensation = 0;
    let fallback: bigint | undefined;
    for (; k < xs.length; k++) {
      const x = xs[k]!;
      if (typeof x === "number") {
        const t = total + x;
        compensation += Math.abs(total) >= Math.abs(x) ? total - t + x : x - t + total;
        total = t;
        continue;
      }
      if (x >= LONG_MIN && x <= LONG_MAX) {
        total += Number(x);
        continue;
      }
      fallback = x;
      k++;
      break;
    }
    if (compensation !== 0 && Number.isFinite(compensation)) total += compensation;
    if (fallback === undefined) return total;
    result = pyAdd(total, fallback);
    if (result === null) return null;
  }
  for (; k < xs.length; k++) {
    result = pyAdd(result, xs[k]!);
    if (result === null) return null;
  }
  return result;
}

function numericFeatures(sample: unknown[]): string[] {
  const feats: string[] = [];
  if (sample.length === 0 || !sample.every(isNumber)) return feats;
  const numbers = sample as (number | bigint)[];
  const zeros = numbers.map((x) => x == 0);
  if (zeros.some(Boolean)) feats.push("any_zero");
  if (zeros.every(Boolean)) feats.push("all_zero");
  if (numbers.some((x) => x < 0)) feats.push("any_neg");
  if (numbers.some((x) => typeof x === "number" && Number.isNaN(x))) feats.push("any_nan");
  if (numbers.some((x) => typeof x === "number" && !Number.isFinite(x) && !Number.isNaN(x))) feats.push("any_inf");
  // Python's `sum(numbers) == 0`; an OverflowError (null) means no sum_zero.
  const total = pythonSum(numbers);
  if (total !== null && total == 0) feats.push("sum_zero");
  return feats;
}

type Pair = [number | bigint, number | bigint];

function numericPair(item: unknown): Pair | null {
  if (classify(item).kind !== "list") return null;
  const arr = item as unknown[];
  if (arr.length !== 2) return null;
  const a = ownData(arr, 0);
  const b = ownData(arr, 1);
  return a.ok && b.ok && isNumber(a.value) && isNumber(b.value) ? [a.value, b.value] : null;
}

function isNaNValue(x: number | bigint): boolean {
  return typeof x === "number" && Number.isNaN(x);
}

function pairFeatures(sample: unknown[]): { feats: string[]; pairs: Pair[] | null } {
  const pairs = sample.map(numericPair);
  if (sample.length === 0 || pairs.some((p) => p === null)) return { feats: [], pairs: null };
  const valid = pairs as Pair[];
  const orders = new Set(valid.map(([a, b]) => (a < b ? "lt" : a == b ? "eq" : a > b ? "gt" : "nan")));
  const label = orders.size === 1 ? [...orders][0]! : "mixed";
  return { feats: ["pair:" + (label === "nan" ? "mixed" : label)], pairs: valid };
}

type Comparable = { values: unknown[]; cmp: (a: unknown, b: unknown) => number; key: (a: unknown) => string };

function numberKey(x: number | bigint): string {
  return typeof x === "number" && Object.is(x, -0) ? "0" : String(x);
}

function compareNumbers(a: number | bigint, b: number | bigint): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function comparable(sample: unknown[], pairs: Pair[] | null): Comparable | null {
  if (pairs !== null) {
    if (pairs.some(([a, b]) => isNaNValue(a) || isNaNValue(b))) return null;
    return {
      values: pairs,
      cmp: (x, y) => {
        const [a1, b1] = x as Pair;
        const [a2, b2] = y as Pair;
        return compareNumbers(a1, a2) || compareNumbers(b1, b2);
      },
      key: (x) => (x as Pair).map(numberKey).join(","),
    };
  }
  if (sample.every(isNumber)) {
    if ((sample as (number | bigint)[]).some(isNaNValue)) return null;
    return {
      values: sample,
      cmp: (a, b) => compareNumbers(a as number, b as number),
      key: (a) => numberKey(a as number),
    };
  }
  if (sample.every((x) => typeof x === "string")) {
    return { values: sample, cmp: (a, b) => compareCodePoints(a as string, b as string), key: (a) => a as string };
  }
  if (sample.every((x) => x !== HOLE && classify(x).kind === "bytes")) {
    const views = sample.map(byteView);
    return { values: views, cmp: (a, b) => compareBytes(a as Uint8Array, b as Uint8Array), key: (a) => bytesKey(a as Uint8Array) };
  }
  return null;
}

const U8 = Uint8Array;

/** The bytes of a `Uint8Array`/`Buffer` (itself) or an `ArrayBuffer` (a view on it). */
function byteView(x: unknown): Uint8Array {
  return utilTypes.isArrayBuffer(x) ? new U8(x) : (x as Uint8Array);
}

/** Python's `bytes` order over the first {@link SCAN_BYTES} bytes (a longer common prefix compares equal). */
function compareBytes(a: Uint8Array, b: Uint8Array): number {
  const la = TYPED_LENGTH.call(a) as number;
  const lb = TYPED_LENGTH.call(b) as number;
  const n = Math.min(la, lb, SCAN_BYTES);
  for (let i = 0; i < n; i++) {
    const d = (a[i] ?? 0) - (b[i] ?? 0);
    if (d !== 0) return d;
  }
  return Math.min(la, lb) <= SCAN_BYTES ? la - lb : 0;
}

function bytesKey(a: Uint8Array): string {
  const length = TYPED_LENGTH.call(a) as number;
  let key = length + ":";
  for (let i = 0; i < Math.min(length, SCAN_BYTES); i++) key += String.fromCharCode(a[i] ?? 0);
  return length > SCAN_BYTES ? key + "…" : key;
}

function orderFeatures(c: Comparable): string[] {
  const { values, cmp, key } = c;
  let sorted = true;
  let reverse = true;
  for (let i = 1; i < values.length; i++) {
    const r = cmp(values[i - 1], values[i]);
    if (r > 0) sorted = false;
    if (r < 0) reverse = false;
  }
  const feats = [sorted ? "order:sorted" : reverse ? "order:reverse" : "order:unsorted"];
  if (new Set(values.map(key)).size < values.length) feats.push("dups");
  return feats;
}

function sequenceFeatures(n: number, sample: unknown[]): string[] {
  const feats = collectionFeatures(n, sample);
  const { feats: pairFeats, pairs } = pairFeatures(sample);
  feats.push(...pairFeats);
  if (sample.length < 2) return feats;
  const c = comparable(sample, pairs);
  feats.push(...(c !== null ? orderFeatures(c) : ["order:n/a"]));
  return feats;
}

function collectionFeatures(n: number, sample: unknown[]): string[] {
  const feats = ["len:" + lenBucket(n)];
  const elem = elemToken(sample);
  if (elem !== null) feats.push(elem);
  feats.push(...numericFeatures(sample));
  return feats;
}

function dictFeatures(n: number, keys: unknown[], values: unknown[]): string[] {
  const feats = collectionFeatures(n, values);
  const key = elemToken(keys);
  if (key !== null) feats.push("key:" + key.slice("elem:".length));
  return feats;
}
