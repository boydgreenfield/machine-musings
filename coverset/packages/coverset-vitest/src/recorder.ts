/**
 * The shape recorder that instrumented code calls (`globalThis.__coverset__`, one per worker).
 *
 * Mirrors `pytest_coverset/shapes.py`:
 *
 * * Sampling is adaptive per function: once {@link SETTLE} consecutive calls produced no new
 *   (parameter, shape) pair, or {@link MAX_SAMPLES} calls were sampled, the function is capped
 *   (`truncated: true`, `cap: "sample"`) and later calls cost a counter check and a stack push.
 * * The caller of a call is the innermost instrumented function still running (a stack pushed by
 *   `enter` and popped by `leave`), else the running test. Async functions and generators are
 *   never on the stack (they suspend, and the stack would then name them as the caller of
 *   unrelated code), so calls they make are attributed to the synchronous caller below them or
 *   to the test. A function that throws leaves its frame until an enclosing instrumented function
 *   returns (which truncates the stack to its own frame) or the test ends (which clears it).
 * * Calls outside a test (module loading) count towards the sampling state but are not written,
 *   as pytest-coverset leaves out `<import>` observations.
 * * Observations are aggregated per (function, parameter, shape, caller, test): the reporter
 *   merges the tests of one caller, and can re-attribute an observation whose caller turns out
 *   not to be measured to its test exactly.
 * * Everything is wrapped so the recorder never throws into user code; failures are counted.
 * * Concurrent tests (`test.concurrent`, `describe.concurrent`, `sequence.concurrent`) interleave
 *   at every `await`, so one "current test" would hand a test's calls to whichever test started
 *   last. For those tests the setup file also enters the test id into an `AsyncLocalStorage`,
 *   whose store follows each test's own async continuations: while more than one test is active
 *   the store says whose call it is. Sequential tests never touch the storage (it is created on
 *   the first concurrent test, so a suite without one pays nothing for it).
 */
import { AsyncLocalStorage } from "node:async_hooks";
import { types as utilTypes } from "node:util";

import { classToken, constructorOf, PROXY_TOKEN, setClassNamer, shapeKey } from "./features.js";
import { type EnumSpec, exampleRepr, RETURN_PARAM } from "./redact.js";

export const MAX_SAMPLES = 2048;
export const SETTLE = 64;
export const RECEIVER = "<receiver>";

const FLAG_RECEIVER = 1;
const FLAG_CONSTRUCTOR = 2;
const FLAG_ASYNC = 4;
const FLAG_GENERATOR = 8;
const FLAG_STATIC = 16;
const MAX_PROTO_HOPS = 16;

/** `src/cart/pricing.ts` → `src.cart.pricing`: the module part of `obj:` tokens. */
export function moduleOf(path: string): string {
  return path.replace(/\.[^./]+$/, "").replace(/\//g, ".");
}

interface FuncState {
  uid: number;
  path: string;
  qualname: string;
  line: number;
  params: string[];
  enums: (EnumSpec | undefined)[];
  flags: number;
  /** Short name of the owning class (`Cart` for `Cart.addItem`), for class members. */
  owner: string | undefined;
  classQualname: string | undefined;
  funcName: string;
  calls: number;
  /** `calls` at the last dump: a dump reports the calls since. */
  dumpedCalls: number;
  sinceNew: number;
  capped: boolean;
  cap: "sample" | null;
  seen: Set<string>;
  tests: Set<string>;
}

type Caller = FuncState | null;

interface Aggregate {
  st: FuncState;
  param: string;
  shape: string;
  caller: { test: string } | { func: number };
  samples: number;
  tests: Set<string>;
  example: string | null;
  exampleTest: string | null;
}

/** What one worker hands the reporter: rows keyed by Vitest task ids (mapped to test ids later). */
export interface ShapeDump {
  funcs: { uid: number; path: string; qualname: string; line: number; calls: number; tests: string[]; capped: boolean; cap: string | null }[];
  obs: {
    f: number;
    p: string;
    s: string;
    c: { test: string } | { func: number };
    n: number;
    t: string[];
    trunc: boolean;
    ex: string | null;
    ext: string | null;
  }[];
  errors: number;
}

interface AsyncToken {
  st: FuncState;
  caller: Caller;
}

export class Recorder {
  maxSamples = MAX_SAMPLES;
  settle = SETTLE;
  errors = 0;
  private readonly files = new Map<string, FuncState[]>();
  private readonly funcs: FuncState[] = [];
  private agg = new Map<string, Aggregate>();
  private test: string | null = null;
  /** Tests between their `beforeEach` and `afterEach`: more than one only while concurrent tests overlap. */
  private readonly active: string[] = [];
  /** Which test an async continuation belongs to; created by the first concurrent test. */
  private als: AsyncLocalStorage<string> | undefined;
  private readonly classes = new WeakMap<Function, string>();
  private stackSt: FuncState[] = [];
  private stackCaller: Caller[] = [];
  private stackSampled: boolean[] = [];
  private depth = 0;

  constructor() {
    setClassNamer((ctor) => this.classes.get(ctor));
  }

  /** Attribute subsequent calls to the Vitest task `taskId` (null between tests); forgets every other active test. */
  setTest(taskId: string | null): void {
    this.test = taskId;
    this.depth = 0;
    this.active.length = 0;
    if (taskId !== null) this.active.push(taskId);
  }

  /**
   * A test starts (its `beforeEach`). A `concurrent` one also enters its id into the async-local
   * store, so its calls stay its own while other tests run between its awaits.
   */
  beginTest(taskId: string, concurrent: boolean): void {
    if (concurrent) {
      try {
        (this.als ??= new AsyncLocalStorage<string>()).enterWith(taskId);
      } catch {
        this.errors++;
      }
    }
    if (!concurrent) this.active.length = 0; // a sequential test never overlaps: drop stale ids
    if (this.active.length === 0) this.depth = 0;
    if (!this.active.includes(taskId)) this.active.push(taskId);
    this.test = taskId;
  }

  /** A test ends (its `afterEach`); the latest test still active, if any, becomes the fallback. */
  endTest(taskId: string): void {
    const at = this.active.indexOf(taskId);
    if (at >= 0) this.active.splice(at, 1);
    this.test = this.active.length ? this.active[this.active.length - 1]! : null;
    if (this.active.length === 0) this.depth = 0;
  }

  /** How many tests are between their `beforeEach` and `afterEach` right now. */
  get activeTests(): number {
    return this.active.length;
  }

  /** The test a call made now belongs to: the async-local one while tests overlap, else the only one. */
  get currentTest(): string | null {
    if (this.active.length <= 1) return this.test;
    const store = this.als?.getStore();
    return store !== undefined && this.active.includes(store) ? store : this.test;
  }

  private lookup(fileId: string, index: number, qualname: string, line: number, spec: string, flags: number): FuncState {
    let table = this.files.get(fileId);
    if (table === undefined) {
      table = [];
      this.files.set(fileId, table);
    }
    let st = table[index];
    if (st === undefined) {
      const parsed = JSON.parse(spec) as { p: string[]; e?: Record<string, EnumSpec> };
      const dot = qualname.lastIndexOf(".");
      const classQualname = flags & FLAG_RECEIVER && dot > 0 ? qualname.slice(0, dot) : undefined;
      st = {
        uid: this.funcs.length,
        path: fileId,
        qualname,
        line,
        params: parsed.p,
        enums: parsed.p.map((_, i) => parsed.e?.[String(i)]),
        flags,
        owner: classQualname?.slice(classQualname.lastIndexOf(".") + 1),
        classQualname,
        funcName: qualname.slice(dot + 1),
        calls: 0,
        dumpedCalls: 0,
        sinceNew: 0,
        capped: false,
        cap: null,
        seen: new Set(),
        tests: new Set(),
      };
      table[index] = st;
      this.funcs.push(st);
    }
    return st;
  }

  /** Called at function entry; returns the token `leave` takes. Never throws. */
  enter(
    fileId: string,
    index: number,
    qualname: string,
    line: number,
    spec: string,
    flags: number,
    args: unknown[],
    self: unknown,
  ): number | AsyncToken {
    let st: FuncState;
    try {
      st = this.lookup(fileId, index, qualname, line, spec, flags);
    } catch {
      this.errors++;
      return -1;
    }
    const caller = this.depth > 0 ? this.stackSt[this.depth - 1]! : null;
    let sampled = false;
    if (!st.capped) {
      if (st.calls >= this.maxSamples || st.sinceNew >= this.settle) {
        st.capped = true;
        st.cap = "sample";
      } else {
        sampled = true;
        try {
          this.sample(st, args, self, caller);
        } catch {
          this.errors++;
        }
      }
    }
    if (flags & (FLAG_ASYNC | FLAG_GENERATOR)) {
      return sampled && !(flags & FLAG_GENERATOR) ? { st, caller } : -1;
    }
    const at = this.depth++;
    this.stackSt[at] = st;
    this.stackCaller[at] = caller;
    this.stackSampled[at] = sampled;
    return at;
  }

  /** Called with every returned value; returns it unchanged. Never throws. */
  leave<T>(token: number | AsyncToken, value: T): T {
    try {
      if (typeof token === "number") {
        if (token < 0 || token >= this.depth) return value;
        const st = this.stackSt[token]!;
        const caller = this.stackCaller[token]!;
        const sampled = this.stackSampled[token]!;
        this.depth = token;
        if (sampled && !st.capped) this.observeReturn(st, value, caller);
      } else if (!token.st.capped) {
        this.observeReturn(token.st, value, token.caller);
      }
    } catch {
      this.errors++;
    }
    return value;
  }

  private observeReturn(st: FuncState, value: unknown, caller: Caller): void {
    if (this.observe(st, RETURN_PARAM, shapeKey(value), caller, value, undefined)) st.sinceNew = 0;
  }

  private sample(st: FuncState, args: unknown[], self: unknown, caller: Caller): void {
    st.calls++;
    const test = this.currentTest;
    if (test !== null) st.tests.add(test);
    let fresh = false;
    for (let i = 0; i < st.params.length; i++) {
      try {
        const value = args[i];
        fresh = this.observe(st, st.params[i]!, shapeKey(value), caller, value, st.enums[i]) || fresh;
      } catch {
        this.errors++;
      }
    }
    if (st.flags & FLAG_RECEIVER) {
      try {
        const receiver = this.receiverToken(st, self);
        if (receiver !== undefined) {
          fresh = this.observe(st, RECEIVER, receiver, caller, NO_EXAMPLE, undefined) || fresh;
        }
      } catch {
        this.errors++;
      }
    }
    st.sinceNew = fresh ? 0 : st.sinceNew + 1;
  }

  /**
   * The `obj:` token of the concrete class of `this` (the class itself for a static member,
   * `new.target` for a constructor), registering the owning class's module on the way so `obj:`
   * tokens of its instances name the defining file. A proxied receiver, prototype or class is
   * `obj:Proxy` and is never read.
   */
  private receiverToken(st: FuncState, self: unknown): string | undefined {
    let concrete: Function | undefined;
    if (st.flags & (FLAG_CONSTRUCTOR | FLAG_STATIC)) {
      concrete = typeof self === "function" ? self : undefined;
    } else if (typeof self === "object" && self !== null) {
      if (utilTypes.isProxy(self)) return PROXY_TOKEN;
      const proto = Object.getPrototypeOf(self) as object | null;
      if (proto !== null && utilTypes.isProxy(proto)) return PROXY_TOKEN;
      concrete = constructorOf(proto);
    }
    if (concrete === undefined) return undefined;
    if (st.owner !== undefined) this.learnClass(st, concrete);
    return classToken(concrete);
  }

  /** Walk from `concrete` up its constructor chain to the class that declares `st`, and name it. */
  private learnClass(st: FuncState, concrete: Function): void {
    let ctor: unknown = concrete;
    for (let hop = 0; hop < MAX_PROTO_HOPS && typeof ctor === "function"; hop++) {
      // A proxied class (or one extending it) is never looked into: classToken names it obj:Proxy.
      if (utilTypes.isProxy(ctor) || this.classes.has(ctor)) break;
      const name = Object.getOwnPropertyDescriptor(ctor, "name");
      if (name !== undefined && "value" in name && name.value === st.owner) {
        this.classes.set(ctor, `${moduleOf(st.path)}.${st.classQualname}`);
        break;
      }
      ctor = Object.getPrototypeOf(ctor);
    }
  }

  private observe(
    st: FuncState,
    param: string,
    shape: string,
    caller: Caller,
    value: unknown,
    enumSpec: EnumSpec | undefined,
  ): boolean {
    const test = this.currentTest;
    if (test !== null) {
      const callerKey = caller === null ? "t" + test : `f${caller.uid}\0${test}`;
      const key = `${st.uid}\0${param}\0${shape}\0${callerKey}`;
      let agg = this.agg.get(key);
      if (agg === undefined) {
        let example: string | null = null;
        if (value !== NO_EXAMPLE) {
          try {
            example = exampleRepr(param, value, { funcName: st.funcName, enumSpec });
          } catch {
            this.errors++;
          }
        }
        agg = {
          st,
          param,
          shape,
          caller: caller === null ? { test } : { func: caller.uid },
          samples: 0,
          tests: new Set(),
          example,
          exampleTest: example === null ? null : test,
        };
        this.agg.set(key, agg);
      }
      agg.samples++;
      agg.tests.add(test);
    }
    const seenKey = `${param}\0${shape}`;
    if (st.seen.has(seenKey)) return false;
    st.seen.add(seenKey);
    return true;
  }

  /**
   * Everything recorded since the last dump. Sampling state (caps, shapes seen) is kept, so a
   * worker that runs several test files keeps sampling adaptively across them; counts restart.
   */
  dump(): ShapeDump {
    const funcs = this.funcs.map((st) => ({
      uid: st.uid,
      path: st.path,
      qualname: st.qualname,
      line: st.line,
      calls: st.calls - st.dumpedCalls,
      tests: [...st.tests],
      capped: st.capped,
      cap: st.cap,
    }));
    const obs = [...this.agg.values()].map((a) => ({
      f: a.st.uid,
      p: a.param,
      s: a.shape,
      c: a.caller,
      n: a.samples,
      t: [...a.tests],
      trunc: a.st.capped,
      ex: a.example,
      ext: a.exampleTest,
    }));
    for (const st of this.funcs) {
      st.dumpedCalls = st.calls;
      st.tests.clear();
    }
    this.agg = new Map();
    const errors = this.errors;
    this.errors = 0;
    return { funcs, obs, errors };
  }
}

const NO_EXAMPLE: unique symbol = Symbol("coverset.no-example");

declare global {
  // eslint-disable-next-line no-var
  var __coverset__: Recorder | undefined;
}

/** The worker's recorder, created on first use. */
export function install(): Recorder {
  globalThis.__coverset__ ??= new Recorder();
  return globalThis.__coverset__;
}
