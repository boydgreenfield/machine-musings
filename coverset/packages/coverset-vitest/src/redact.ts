/**
 * Redacted example reprs: a port of `pytest_coverset/redact.py` to JavaScript values.
 *
 * The repr is the reporter's own, JavaScript-flavoured (`"eu"`, `[1, 2]`, `{"a": 1}`,
 * `Currency.EUR` for an enum member), shortened like Python's `reprlib` with `maxstring=48,
 * maxother=48, maxlevel=2` (6 list and set items, 4 dict items, 40 digits of a bigint): a long
 * string keeps its first 22 and last 23 characters around `...`. A value is redacted to
 * `<redacted:len=N>` (`<redacted>` when it has no length) when the name it is bound to looks
 * secret: the parameter name, the function name for the pseudo-parameter `return`, or a string
 * key inside an object example. Every string is scrubbed with the control plane's credential
 * patterns *before* it is shortened, and the finished text once more; an email keeps its domain
 * and the first character of its local part (`b***@acme.com`). Class instances print as
 * `<module.Class>` inside containers and are not recorded at the top level. No user code runs:
 * values are read as `features.ts` reads them.
 */
import { types as utilTypes } from "node:util";

import { classToken, codePointLength, constructorOf } from "./features.js";

export const MAX_EXAMPLE_CHARS = 200;
export const MAX_INT_BITS = 128;
export const RETURN_PARAM = "return";
const MAX_STRING = 48;
const MAX_OTHER = 48;
const MAX_LONG = 40;
const MAX_LEVEL = 2;
const MAX_LIST = 6;
const MAX_DICT = 4;
const MAX_SET = 6;
const FILL = "...";
export const SCRUB_WINDOW = 4096;

export const SECRET_NAME_RE = /pass|secret|token|key|auth|cookie|session|cred/i;

const WS = "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";

/** Credential shapes masked in example text (kept in sync with `coverset.logging.PATTERNS`). */
export const SECRET_PATTERNS: ReadonlyArray<[RegExp, string]> = [
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, "[REDACTED PRIVATE KEY]"],
  [new RegExp(`(x-access-token:)[^@${WS}]+@`, "giu"), "$1[REDACTED]@"],
  [/\b(gh[opsur]_)[A-Za-z0-9._-]{20,}/g, "$1[REDACTED]"],
  [/\b(github_pat_)[A-Za-z0-9_]{20,}/g, "$1[REDACTED]"],
  [/\b(sk-ant-)[A-Za-z0-9_-]{8,}/g, "$1[REDACTED]"],
  [new RegExp(`\\b(authorization:[${WS}]*(?:bearer|token)[${WS}]+)[^${WS}"',;]+`, "giu"), "$1[REDACTED]"],
];

const EMAIL_LOCAL_RE = new RegExp(
  `([^${WS}@'"\\\\,(){}\\[\\]<>])[^${WS}@'"\\\\,(){}\\[\\]<>]*@(?=[^${WS}@'"]+\\.[^${WS}@'"]+)`,
  "gu",
);

/** A JSON-safe string: lone surrogates become U+FFFD (the bundle format forbids them). */
export function wellFormed(text: string): string {
  return text.toWellFormed();
}

/** Mask credential shapes and email local parts in `text`. */
export function scrub(text: string): string {
  for (const [pattern, replacement] of SECRET_PATTERNS) text = text.replace(pattern, replacement);
  return text.replace(EMAIL_LOCAL_RE, "$1***@");
}

/** Scrub a raw string before it is shortened (two windows for a very long one, as in Python). */
function scrubRaw(text: string): string {
  if (text.length <= 2 * SCRUB_WINDOW || text.includes("PRIVATE KEY-----")) return scrub(text);
  return scrub(text.slice(0, SCRUB_WINDOW)) + " " + scrub(text.slice(-SCRUB_WINDOW));
}

function cutMiddle(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const i = Math.max(0, Math.floor((limit - 3) / 2));
  const j = Math.max(0, limit - 3 - i);
  return text.slice(0, i) + FILL + text.slice(text.length - j);
}

function reprString(raw: string): string {
  const x = scrubRaw(raw);
  let s = JSON.stringify(x.slice(0, MAX_STRING));
  if (s.length > MAX_STRING) {
    const i = Math.max(0, Math.floor((MAX_STRING - 3) / 2));
    const j = Math.max(0, MAX_STRING - 3 - i);
    s = JSON.stringify(x.slice(0, i) + x.slice(x.length - j));
    s = s.slice(0, i) + FILL + s.slice(s.length - j);
  }
  return s;
}

function bitLength(x: bigint): number {
  return (x < 0n ? -x : x).toString(2).length;
}

function reprNumber(x: number): string {
  return cutMiddle(Object.is(x, -0) ? "-0" : String(x), MAX_OTHER);
}

function reprBigint(x: bigint): string {
  const bits = bitLength(x);
  if (bits > MAX_INT_BITS) return `<int ${bits} bits>`;
  return cutMiddle(String(x), MAX_LONG);
}

function dataValue(obj: object, key: PropertyKey): { ok: boolean; value: unknown } {
  const desc = Object.getOwnPropertyDescriptor(obj, key);
  if (desc === undefined) return { ok: true, value: undefined };
  return "value" in desc ? { ok: true, value: desc.value } : { ok: false, value: undefined };
}

type Shape = "scalar" | "list" | "dict" | "map" | "set" | "bytes" | "other";

function shapeOf(x: unknown): Shape {
  if (x === null || (typeof x !== "object" && typeof x !== "function")) {
    return typeof x === "symbol" ? "other" : "scalar";
  }
  if (typeof x === "function" || utilTypes.isProxy(x)) return "other";
  if (utilTypes.isUint8Array(x) || utilTypes.isArrayBuffer(x)) return "bytes";
  const proto = Object.getPrototypeOf(x) as object | null;
  if (Array.isArray(x) && proto === Array.prototype) return "list";
  if (proto === Object.prototype || proto === null) return "dict";
  if (proto === Map.prototype && utilTypes.isMap(x)) return "map";
  if (proto === Set.prototype && utilTypes.isSet(x)) return "set";
  return "other";
}

const TYPED_LENGTH = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(Uint8Array.prototype), "length")!.get!;
const ARRAY_BUFFER_LENGTH = Object.getOwnPropertyDescriptor(ArrayBuffer.prototype, "byteLength")!.get!;
const MAP_SIZE = Object.getOwnPropertyDescriptor(Map.prototype, "size")!.get!;
const SET_SIZE = Object.getOwnPropertyDescriptor(Set.prototype, "size")!.get!;

function sizeOf(x: unknown, shape: Shape): number | null {
  switch (shape) {
    case "list":
      return (x as unknown[]).length;
    case "dict":
      return Object.keys(x as object).length;
    case "map":
      return MAP_SIZE.call(x) as number;
    case "set":
      return SET_SIZE.call(x) as number;
    case "bytes":
      return (utilTypes.isArrayBuffer(x) ? ARRAY_BUFFER_LENGTH.call(x) : TYPED_LENGTH.call(x)) as number;
    default:
      return typeof x === "string" ? codePointLength(x) : null;
  }
}

function redacted(value: unknown): string {
  const n = sizeOf(value, shapeOf(value));
  return n === null ? "<redacted>" : `<redacted:len=${n}>`;
}

function items(parts: string[], n: number, limit: number, left: string, right: string): string {
  if (n > limit) parts.push(FILL);
  return left + parts.join(", ") + right;
}

function valueRepr(key: unknown, value: unknown, level: number): string {
  if (typeof key === "string" && SECRET_NAME_RE.test(key)) return redacted(value);
  return repr1(value, level);
}

function repr1(x: unknown, level: number): string {
  switch (typeof x) {
    case "string":
      return reprString(x);
    case "number":
      return reprNumber(x);
    case "bigint":
      return reprBigint(x);
    case "boolean":
      return String(x);
    case "undefined":
      return "undefined";
  }
  if (x === null) return "null";
  const shape = shapeOf(x);
  switch (shape) {
    case "list": {
      const arr = x as unknown[];
      if (arr.length === 0) return "[]";
      if (level <= 0) return "[" + FILL + "]";
      const parts: string[] = [];
      for (let i = 0; i < Math.min(arr.length, MAX_LIST); i++) {
        const item = dataValue(arr, i);
        parts.push(item.ok ? repr1(item.value, level - 1) : "<accessor>");
      }
      return items(parts, arr.length, MAX_LIST, "[", "]");
    }
    case "dict": {
      const keys = Object.keys(x as object);
      if (keys.length === 0) return "{}";
      if (level <= 0) return "{" + FILL + "}";
      const parts = keys.slice(0, MAX_DICT).map((k) => {
        const item = dataValue(x as object, k);
        return `${reprString(k)}: ${item.ok ? valueRepr(k, item.value, level - 1) : "<accessor>"}`;
      });
      return items(parts, keys.length, MAX_DICT, "{", "}");
    }
    case "map": {
      const n = MAP_SIZE.call(x) as number;
      if (n === 0) return "Map {}";
      if (level <= 0) return "Map {" + FILL + "}";
      const parts: string[] = [];
      const it = Map.prototype.entries.call(x as Map<unknown, unknown>);
      for (let i = 0; i < MAX_DICT; i++) {
        const step = it.next();
        if (step.done) break;
        const [k, v] = step.value;
        parts.push(`${repr1(k, level - 1)} => ${valueRepr(k, v, level - 1)}`);
      }
      return items(parts, n, MAX_DICT, "Map {", "}");
    }
    case "set": {
      const n = SET_SIZE.call(x) as number;
      if (n === 0) return "Set {}";
      if (level <= 0) return "Set {" + FILL + "}";
      const parts: string[] = [];
      const it = Set.prototype.values.call(x as Set<unknown>);
      for (let i = 0; i < MAX_SET; i++) {
        const step = it.next();
        if (step.done) break;
        parts.push(repr1(step.value, level - 1));
      }
      return items(parts, n, MAX_SET, "Set {", "}");
    }
    case "bytes": {
      const n = sizeOf(x, "bytes")!;
      if (utilTypes.isArrayBuffer(x)) return `ArrayBuffer(${n})`;
      const view = x as Uint8Array;
      const parts: string[] = [];
      for (let i = 0; i < Math.min(n, MAX_LIST); i++) parts.push(String(Uint8Array.prototype.at.call(view, i)));
      return items(parts, n, MAX_LIST, `Uint8Array(${n}) [`, "]");
    }
    default:
      return "<" + otherName(x) + ">";
  }
}

function otherName(x: unknown): string {
  if (typeof x === "function") return "Function";
  if (typeof x === "symbol") return "Symbol";
  if (utilTypes.isProxy(x)) return "Proxy";
  return classToken(constructorOf(Object.getPrototypeOf(x) as object | null)).slice("obj:".length);
}

/** An enum declared for a parameter: its name and member name by member value. */
export interface EnumSpec {
  name: string;
  members: Record<string, string | number>;
}

/** The member of `spec` whose value is `value`, as `Name.MEMBER`, or null. */
export function enumMember(spec: EnumSpec, value: unknown): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  for (const [member, memberValue] of Object.entries(spec.members)) {
    if (memberValue === value) return `${spec.name}.${member}`;
  }
  return null;
}

/**
 * A short redacted repr of `value` for parameter `param`, or null when not recorded (a class
 * instance, function or symbol at the top level, as Python records builtin values only).
 */
export function exampleRepr(
  param: string,
  value: unknown,
  options: { funcName?: string; enumSpec?: EnumSpec } = {},
): string | null {
  const shape = shapeOf(value);
  if (shape === "other") return null;
  const name = param === RETURN_PARAM && options.funcName !== undefined ? options.funcName : param;
  if (SECRET_NAME_RE.test(name)) return redacted(value);
  if (options.enumSpec !== undefined) {
    const member = enumMember(options.enumSpec, value);
    if (member !== null) return member;
  }
  return wellFormed(scrub(repr1(value, MAX_LEVEL)).slice(0, MAX_EXAMPLE_CHARS));
}

/** A failure message as `tests.jsonl` keeps it: scrubbed and cut at 8 KiB. */
export function redactMessage(text: string, limit = 8192): string {
  const scrubbed = scrub(text.length > 4 * limit ? text.slice(0, 4 * limit) : text);
  return wellFormed(scrubbed.length > limit ? scrubbed.slice(0, limit - 1) + "…" : scrubbed);
}
