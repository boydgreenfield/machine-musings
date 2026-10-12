# TypeScript: the Vitest reporter

`coverset-vitest` (`packages/coverset-vitest`) is a Vitest reporter that writes coverset's
[neutral bundle](neutral-bundle.md) from a real test run: which tests ran and how they ended, the
lines and branch arcs each test executed, the function regions with their declared types, and the
shapes of the values that reached every function's parameters. Everything downstream (`coverset
bundle validate`, the facts store, the input-class catalogue, the static report, the live pages)
reads that bundle exactly as it reads one from `pytest-coverset`. `examples/ts-app` is a small cart
library whose `npm test` writes one, and `tests/e2e/test_vitest_reporter.py` runs it, validates the
bundle, ingests it and builds the report.

It has three parts, all exported by the package:

| Export | Where it runs | What it does |
|---|---|---|
| `coversetPlugin()` | Vite (main process), `enforce: "pre"` | instruments every function of the measured sources so the shape recorder sees its arguments and return values |
| `coversetSetup` | every test worker (`setupFiles`) | diffs Istanbul's counters around each test and spools the lines, arcs and shape observations |
| `coversetReporter()` | the Vitest main process | merges the spools with Vitest's results and Istanbul's coverage map and writes `.coverset/neutral/` |

## Setup

```sh
npm install --save-dev vitest@5.0.3 @vitest/coverage-istanbul@5.0.3 coverset-vitest
```

```ts
// vitest.config.ts
import { coversetPlugin, coversetReporter, coversetSetup } from "coverset-vitest";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [coversetPlugin({ include: ["src"] })],
  test: {
    setupFiles: [coversetSetup],
    includeTaskLocation: true,
    reporters: ["default", coversetReporter()],
    coverage: {
      enabled: true,
      provider: "istanbul",
      include: ["src/**/*.ts"],
      reporter: ["text-summary"],
    },
  },
});
```

* `coverage.provider` must be `"istanbul"`: the per-test lines come from its counters, and its
  statement map gives `files.jsonl`. `coverage.include` is what is measured; Vitest 5 has no
  `coverage.all` (an `include` already reports files no test loaded). The `v8` provider is not
  supported.
* `setupFiles` and `includeTaskLocation` are added by the plugin if missing; listing them keeps the
  config readable. The setup file must run first, so the plugin puts it at the front.
* No `pool` setting is needed: the default `forks` pool with `isolate: true`, `threads` and
  `isolate: false` all work (each worker appends to its own spool file, and a counter reset
  between test files in a reused worker is detected).
* Invariants and property tests are declared through Vitest's test metadata,
  `it("takes 10% off", { meta: { invariant: "discount_never_negative" } }, () => ...)`; the package
  adds `invariant`, `invariants` and `property` to Vitest's `TaskMeta` type.
* A run with coverage switched off (`--coverage.enabled=false`) prints a warning and writes no
  bundle; it does not fail the run.
* With npm 10.9, installing `vitest@5` can fail inside npm itself (`Cannot read properties of null
  (reading 'edgesOut')`, an arborist bug with Vitest's optional peer dependencies); the package and
  the example carry `legacy-peer-deps=true` in `.npmrc` and list `vite` explicitly.

`npx vitest --run` then prints `coverset-vitest: wrote .../.coverset/neutral (N tests, ...)`, and

```sh
uv run coverset bundle validate .coverset/neutral
uv run coverset report .coverset/neutral --repo-root "$(git rev-parse --show-toplevel)" --out .coverset/report
```

check it and build the static report (the Python side: `uv sync` in this repository).

### Paths

Every path in the bundle (sources, test files, test ids) is relative to the repository root: the
`root` option of the plugin and the reporter, else `COVERSET_ROOT`, else the git work tree
(`git rev-parse --show-toplevel`, or the nearest directory holding `.git`). In this repository the
example's files are therefore `coverset/examples/ts-app/src/pricing.ts`. The report reads sources
only under `--repo-root` and, in a git work tree, only files git tracks there, so pass the same
root the reporter used (`$(git rev-parse --show-toplevel)`, what `npm run report` does); `--repo-root .`
from the example would look for `./coverset/examples/...` and show no source.

| Variable | Effect |
|---|---|
| `COVERSET_ROOT` | the repository root paths are relative to (overrides git) |
| `COVERSET_OUT` | the output directory (default `.coverset/neutral` under Vitest's root) |
| `COVERSET_SHAPES=0` | no instrumentation: tests, coverage and regions only, and an empty `shapes.jsonl` |
| `COVERSET_SHARD` | the manifest's `shard` (default: Vitest's `--shard` index minus one, else `null`) |
| `COVERSET_SPOOL` | where workers spool (set by the reporter to `.coverset/spool` under Vitest's root) |

## What it records

| File | From |
|---|---|
| `manifest.json` | `language: "typescript"`, producer `coverset-vitest` and its version; `meta` holds the Vitest, Node and TypeScript-API versions, the recorder's error count, what was dropped and any warning |
| `tests.jsonl` | Vitest's test cases. `id` is `<test file> > <describe> > <name>` (Vitest's `fullName` behind the repository path of the file). `passed` → `passed`, `failed` → `failed`, `skip` and `todo` → `skipped` (with a `skip` or `todo` marker), a `test.fails` test that failed as expected → `xfailed`, a test that never ran → `null`. `duration_s`, `start`, `stop` from Vitest's diagnostics; `line` from the task location. A failure's `message` is `<error name>: <message>`, scrubbed of credentials and cut at 8 KiB; `fail_sig` is the first 16 hex digits of sha256 over the error name and the first stack frame inside the repository (`path:line`), never the message. `markers` are the test's tags plus its non-`notice` annotation types; `invariants` come from `meta.invariant(s)`; `property` is true for `meta.property`, a `property` tag, or a test whose body calls `fc.assert`/`fc.property`/`fc.asyncProperty` or `test.prop` (fast-check) |
| `files.jsonl` | Istanbul's coverage map after Vitest remapped it to the original sources: `statements` are the first lines of its statements, `branches` the `[branch line, target line]` pairs of its branch map (below), `excluded: []`, `n_lines` the file's line count. Every file of `coverage.include` is listed, tested or not |
| `coverage.jsonl` | per (test, file), the statement lines whose counters moved during the test (its `beforeEach`/`afterEach` hooks included), with the branch arcs that moved; everything outside a test (module loading, `describe` bodies, `beforeAll`) is `<import>` |
| `arcs.jsonl` | the branch arcs whose run-wide counters are non-zero |
| `functions.jsonl` | regions from the TypeScript compiler API (below), with `body_hash`/`sig_hash` and the declared types as descriptors; no `key` (the reader computes it) |
| `shapes.jsonl` | the recorder's observations (`obs` rows with redacted examples) and one `sampling` row per function it saw |
| `links.jsonl` | one row per `meta.invariant` of a test |

### Per-test coverage from Istanbul's counters

Vitest's Istanbul provider instruments each measured module in the worker and keeps its counters
in `globalThis.__VITEST_COVERAGE__`, with statement and branch locations in the transformed code
and the source map back to the original file. The setup file reads those counters (it never resets
them, so Vitest's own report is untouched): `beforeEach` closes the previous checkpoint into
`<import>`, `afterEach` diffs the counters against it, maps every statement whose counter moved to
its original first line through the source map (as `istanbul-lib-source-maps` does: greatest lower
bound, then least upper bound), and appends `{task, files: {path: {lines, arcs}}}` to the worker's
spool. The reporter keeps only lines that are statements of the remapped coverage map (and counts
any other line in `meta.dropped`, empty on the example). The cost is one pass over each loaded
file's counters per test.

Istanbul gives an `if` two branch locations: the whole statement and the `else` part (empty
without an `else`). The arcs are those coverage.py would draw: the `if` line to the first statement
of the consequent, to the first statement of the `else`, and, without an `else`, to the first
statement after the `if` (or the negated `if` line when nothing follows, an exit). A
`cond-expr`, `binary-expr` (`&&`, `||`, `??`), `switch` case or default argument goes to the first
statement inside the location, else to its first line, so `a ?? b` on one line is the arc `[L, L]`.

### Function regions and declared types

The same indexer serves the plugin (so shape rows and regions agree on qualnames and first lines)
and the reporter:

* a region per function declaration, function expression, arrow function, method, accessor,
  constructor and class with a body, and one `<module>` region per file; overload signatures,
  `declare` and abstract members are not regions;
* `qualname` joins the enclosing names with `.`: `Cart.addItem`, `outer.inner`, `api.get` for a
  method of `const api = {...}`, `N.f` inside `namespace N`; a function expression or arrow takes
  the name it is bound to (variable, class field, property, `export default`), else its own name,
  else `<anonymous>`; `ordinal` separates same-named regions (a getter and its setter, two
  callbacks in one function);
* `def_line` is the declaration's first line (decorators and `export` included; the `const` line
  for a bound arrow), `body_start` the first statement of the body (a JSDoc or comment is not one;
  the `{` line of an empty body; the expression of an expression-bodied arrow; a class's first
  member), `end_line` the closing brace; `kind` is `method` for class members (arrow-function class
  fields included), `class`, `module`, else `function`;
* `params` are the declared parameters without `this`: `...rest` keeps its prefix, a destructured
  parameter is `arg<i>`;
* `body_hash` and `sig_hash` are sha256 over the tokens (comments and layout ignored) of the body
  and of the declaration up to the body; a class or module hash leaves out the bodies of the
  functions and classes directly inside it, so editing a method changes the method's hash only.

Types come from a TypeScript program over the measured files with the compiler options of the
nearest `tsconfig.json`, through the type checker, for parameters and return types that are
**annotated** (an unannotated or contextually typed parameter is `null`, as in Python):

| Declared type | Descriptor |
|---|---|
| `number` | `float` (integral values are still observed as `int`) |
| `bigint` | `int` |
| `string`, `boolean` | `str`, `bool` |
| `null`, `undefined`, `void` | `none`; `T \| undefined` and an optional `x?: T` are a union with `none` |
| `"eu" \| "world"`, `1 \| 2 \| 3`, `true` | `literal` with the values (a numeric literal union of integers is a literal, not `float`) |
| an `enum` (or every member of one in a union) | `enum` with the member names; some members only are a `literal` of their values |
| `T[]`, `Array<T>`, `readonly T[]`, `ReadonlyArray<T>`, `Iterable<T>` | `list` with `elem` |
| `[A, B]` | `tuple` with `elems`; `[A, ...B[]]` a variable `tuple` with `elem` |
| `Set<T>` | `set`; `Map<K, V>`, `Record<K, V>`, `{ [k: string]: V }` → `dict` |
| `Uint8Array`, `Buffer`, `ArrayBuffer` | `bytes` |
| a class or interface | `object`, named `<module>.<Name>` when declared in the repository (`coverset.examples.ts-app.src.pricing.Cart`), else the bare name; a function type is `object` `Function` |
| `any`, `unknown`, `never`, a type parameter, an intersection without a primitive | `any` (a branded `number & {...}` is `float`) |

An `async` function's return descriptor is the awaited type. The program is built once per run
with the reporter's own TypeScript (6.0.3, the last release with the JavaScript compiler API;
TypeScript 7's API is native and still `unstable/`), whatever version the project compiles with:
the example type-checks with TypeScript 7.0.2.

## Shapes: the instrumentation

`coversetPlugin` parses each measured file with the TypeScript parser before Vite strips the types,
finds the same regions, and inserts text with magic-string. The body of
`examples/ts-app/src/shipping.ts` becomes (the line breaks are the original ones):

```ts
export function rateFor(zone: Zone, weightsGrams: number[]): number { /* istanbul ignore next -- @preserve */ const __cs$1 = globalThis.__coverset__.enter("src/shipping.ts", 1, "rateFor", 6, "{\"p\":[\"zone\",\"weightsGrams\"]}", 0, [zone, weightsGrams], undefined);
  const grams = weightsGrams.reduce((sum, g) => globalThis.__coverset__.leave(globalThis.__coverset__.enter("src/shipping.ts", 2, "rateFor.<anonymous>", 7, "{\"p\":[\"sum\",\"g\"]}", 0, [sum, g], undefined), (sum + g)), 0);
  const extraKg = Math.max(0, Math.ceil((grams - 1000) / 1000));
  return globalThis.__coverset__.leave(__cs$1, (BASE_CENTS[zone] + extraKg * 200));
; /* istanbul ignore next -- @preserve */ globalThis.__coverset__.leave(__cs$1, undefined); }
```

`enter(fileId, index, qualname, firstLine, spec, flags, args, self)` takes the repository path,
the region's index in the file, its qualname and `def_line` (so shape rows match
`functions.jsonl`), a JSON string naming the recorded parameters (and the enum of an enum-typed
one), flags (class member, constructor, async, generator, static), the parameter values and the
receiver; it returns the token `leave(token, value)` takes.

* **Lines do not move.** Only text without line breaks is inserted, and the source map is
  magic-string's high-resolution map, so Vite, Istanbul and stack traces keep the original lines
  and columns (`test/instrument.test.ts` checks every line of a sample).
* **Istanbul counts the same.** Each inserted statement carries `/* istanbul ignore next */`, and
  expression-bodied arrows get no statement at all, so statements, branches, functions and every
  per-test line are identical with and without the plugin: the end-to-end test runs the example
  both ways (`COVERSET_SHAPES=0`) and compares `files`, `coverage`, `arcs` and `functions`
  byte for byte.
* **Behaviour does not change.** `enter` receives the parameter values after defaults (a
  destructured parameter through `arguments[i]`, not recorded in an arrow function), `this` for
  class members and `new.target` for constructors (so a derived constructor never touches `this`
  before `super()`); `leave` returns the value it is given. Both catch everything and count
  failures in `meta.recorder_errors`; a sample covering directives, ASI, derived classes,
  generators, async functions, curried arrows and comma expressions computes the same results
  instrumented and plain.
* **No user code runs.** Shape keys and examples read values with `typeof`, `Array.isArray`,
  `Object.getPrototypeOf`, own data properties (an accessor is never invoked; it is
  `obj:accessor`), `Object.keys` and the intrinsic iterators of exact `Map`/`Set` instances; a
  `Proxy` is `obj:Proxy` and is never looked into; `toString`, `valueOf` and getters are never
  called (tested).

The recorder (`globalThis.__coverset__`, one per worker) ports `pytest_coverset/shapes.py`:

* **Adaptive sampling.** A function stops being sampled once 64 consecutive calls brought no new
  (parameter, shape) pair, or after 2,048 calls; its `sampling` row then says `truncated: true,
  cap: "sample"`, and later calls cost a counter check and a stack push (about 11 ns, against
  about 0.8 µs for a sampled call). There is no full-trace list.
* **Callers.** A call's caller is the innermost instrumented function still on the recorder's
  stack, else the running test (`{"test": id}`); the example's `reduce` callback is recorded as
  called from `rateFor`.
* **Receivers.** Class members record `<receiver>`, the concrete class of `this` (the class for a
  static member, `new.target` for a constructor). The first time a method of class `Cart` in
  `src/pricing.ts` runs, the recorder learns that `Cart` is `src.pricing.Cart`, so instances of it
  are `obj:src.pricing.Cart` wherever they appear; a class none of whose methods ran yet is
  `obj:Cart`.
* **Returns.** Every `return`, a bare `return` and falling off the end are recorded (`undefined`
  is `NoneType`, as Python's implicit `None`); an `async` function records the value it returns
  before the promise wraps it; a generator records its arguments only.
* **Import time.** Calls outside a test count towards sampling but are not written, as
  pytest-coverset leaves out `<import>` observations.

### Values to shape keys

| JavaScript value | Key |
|---|---|
| `true`, `false` | `bool\|true`, `bool\|false` |
| a number with `Number.isInteger(n)`, a `bigint` | `int` with `neg`/`zero`/`pos`, `big` beyond ±2³¹ (`-0` is `int\|zero`) |
| any other number | `float` with `nan`, `inf` (+ `inf-neg`), else `neg`/`zero`/`pos`; never `integral` |
| a string | `str` with `len:<bucket>` (in code points), `empty`, `whitespace`, `ascii`/`unicode`, `ws-edge`, `digits` and `kind:` by Python's regular expressions (Python's whitespace set and Unicode digits): `email/<plain\|multi-label-domain\|plus-tag\|uppercase\|idn>`, `numeric`, `quantity/<...>`, `iso-date`, `url` |
| `null`, `undefined` | `NoneType` |
| an array (exactly `Array`) | `list` with `len`, `elem`, `any_zero`/`all_zero`/`any_neg`/`any_nan`/`any_inf`/`sum_zero` (Python 3.12's compensated float sum), `order`, `dups`, `pair` over the first 32 elements |
| a plain object (or `Object.create(null)`), a `Map` | `dict` with `len`, `elem` (values), `key` (keys) |
| a `Set` | `set` with `len`, `elem`, the numeric summaries |
| `Uint8Array`, `Buffer`, `ArrayBuffer` | `bytes\|len:<bucket>` |
| a class instance, a subclass of `Array`/`Map`/`Set` | `obj:<module>.<Class>` (or `obj:<Class>`) |
| a function, a symbol, a proxy | `obj:Function`, `obj:Symbol`, `obj:Proxy` |

`test/features.test.ts` checks 55 values against the keys `pytest_coverset.features.shape_key`
returns for the same values in Python (generated by running it) and the 22 string-kind cases of the
Python plugin's tests.

### Examples and redaction

One example per (function, parameter, shape, caller), JavaScript-flavoured and shortened like
Python's `reprlib` (48 characters of a string, its first 22 and last 23 around `...`; 6 list and
set items, 4 dict items, two levels): `"eu"`, `[1, 2]`, `{"a": 1}`, `Map {"k" => 1}`. A value of a
parameter typed with an `enum` declared in the same file or imported from a relative module is
shown as its member, `Currency.EUR`, so the catalogue's enum classes open and match. The redaction
is the Python plugin's: a parameter (or, for `return`, a function) named like
`pass|secret|token|key|auth|cookie|session|cred` shows `<redacted:len=N>`, so does such a key
inside an object; credential patterns (private keys, `ghp_…`, `github_pat_…`, `sk-ant-…`,
`Authorization: Bearer …`, `x-access-token:…@`) are masked before shortening and again after; an
email keeps its first character and its domain (`b***@acme.com`). Class instances, functions and
symbols get no top-level example (`<Class>` inside a container). Lone surrogates never reach the
bundle.

## Overhead

`examples/ts-app` (20 tests, 5 files), median of 5 runs after a warm-up, Node 23.11, Vitest 5.0.3,
on the machine this was written on:

| Configuration | Wall time of `vitest --run` | Vitest's "Duration" |
|---|---|---|
| plain Vitest, no coverage | 428 ms | 90 ms |
| Istanbul coverage only | 571 ms | 153 ms |
| coverset-vitest, `COVERSET_SHAPES=0` | 973 ms | 164 ms |
| coverset-vitest with shapes | 982 ms | 316 ms |

The ~0.4 s over Istanbul alone is fixed per run, not per test: loading the TypeScript compiler
(once, in Vitest's main process, shared by the plugin and the reporter) and type-checking the
measured files for `functions.jsonl`. With shapes on, the compiler loads during the first
transform, so Vitest's "Duration" counts it while the wall time barely moves. Per call, an instrumented function
costs about 0.8 µs while sampled and about 11 ns once capped (a two-argument function, Node 23).
The per-test counter diff is linear in the statements of the loaded files.

## Limits and caveats

* **Source maps.** The plugin runs with `enforce: "pre"` and sees the file as read from disk; a
  plugin that rewrites the source before it (another `pre` plugin earlier in the list) would make
  its lines disagree with Istanbul's. Lines are original lines; columns are not used.
* **Async code.** Async functions and generators are recorded but never act as callers (they
  suspend, and a stack cannot say who resumed them): calls they make are attributed to the
  synchronous instrumented caller below them or to the test. Tests that run concurrently
  (`test.concurrent`, `describe.concurrent`) share one current test, so their lines and shapes are
  attributed to whichever test was set last; run them sequentially for per-test facts.
* **Exceptions.** A function that throws leaves its frame on the caller stack until an enclosing
  instrumented function returns or the test ends, so a call made from a `catch` block in between
  can name the thrower as its caller.
* **Hooks and phases.** A failing `beforeEach` fails the test in Vitest; it is reported `failed`
  with `failed_phase: "call"`, not `error`. `beforeAll`/`afterAll` code is `<import>`.
* **Not recorded.** `excluded` lines (always `[]`), per-test arcs are written but not read by
  coverset, types of unannotated or contextually typed parameters (`null`), enum examples for
  enums imported from packages, `obj:` modules of classes none of whose methods ran, Vue/Svelte
  single-file components (only `.ts`, `.tsx`, `.mts`, `.cts`, `.js`, `.jsx`, `.mjs`, `.cjs` are
  instrumented), browser mode (the spool is a file).
* **Python-isms.** `dataclass`/`pydantic` object features never appear, and numbers that happen to
  be integral are `int`, so `[1.0, 2.0]` is `list|elem:int`.
* **Fixed costs.** Loading TypeScript and type-checking the measured files cost about 0.4 s per
  run on the example; `coversetReporter({ types: false })` skips the checker (no `types` in
  `functions.jsonl`).
* **Watch mode** rewrites the bundle after every run, from that run's tests only.
* **Development.** The example installs the package as a copy (`install-links=true`, so Vitest is
  the example's own and not the package's), so after changing the package run
  `npm run refresh-reporter` in `examples/ts-app` (build, then reinstall).
