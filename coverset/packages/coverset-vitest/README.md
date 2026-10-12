# coverset-vitest

A Vitest reporter that writes coverset's [neutral bundle](../../docs/neutral-bundle.md) from a real
test run: per-test lines and branch arcs from Istanbul's counters, function regions with their
declared types from the TypeScript checker, and the shapes of the values each function received,
recorded by a small instrumentation plugin. [docs/typescript.md](../../docs/typescript.md) says what
it records and how; `examples/ts-app` uses it.

## Quick start

```ts
// vitest.config.ts  (npm i -D vitest@5.0.3 @vitest/coverage-istanbul@5.0.3 coverset-vitest)
import { coversetPlugin, coversetReporter, coversetSetup } from "coverset-vitest";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [coversetPlugin({ include: ["src"] })],
  test: {
    setupFiles: [coversetSetup],
    reporters: ["default", coversetReporter()],
    coverage: { enabled: true, provider: "istanbul", include: ["src/**/*.ts"] },
  },
});
```

`npx vitest --run` writes `.coverset/neutral/`; `uv run coverset bundle validate .coverset/neutral`
checks it and `uv run coverset report .coverset/neutral --repo-root "$(git rev-parse --show-toplevel)"`
renders it (both from this repository's Python side).

## Layout

| File | Role |
|---|---|
| `src/plugin.ts` | `coversetPlugin()`: the Vite plugin (instruments measured sources, adds the setup file) |
| `src/instrument.ts` | the source transform: `enter`/`leave` calls, Istanbul ignore hints, enum lookup |
| `src/regions.ts` | function regions (qualnames, ordinals, lines), shared by the plugin and the reporter |
| `src/setup.ts` | `coversetSetup`: per-test counter diffs and the worker's spool |
| `src/counters.ts`, `src/branches.ts` | Istanbul counters → original lines and branch arcs |
| `src/recorder.ts` | the shape recorder (`globalThis.__coverset__`): adaptive cap, callers, receivers |
| `src/features.ts`, `src/redact.ts` | shape keys and redacted examples (ports of `pytest_coverset`'s) |
| `src/descriptors.ts`, `src/functions.ts` | type descriptors through the checker; `functions.jsonl` rows and hashes |
| `src/reporter.ts` | `coversetReporter()`: merges everything and writes the bundle |

## Development

```sh
npm install        # .npmrc sets legacy-peer-deps (npm 10.9 trips over Vitest 5's optional peers)
npm run build      # tsc → dist/
npm test           # vitest: shape keys against pytest-coverset, redaction, regions, types, instrumentation
npm run typecheck  # tsc --noEmit over src and test
```

Dependencies are pinned: `typescript` 6.0.3 (the last release with the JavaScript compiler API;
TypeScript 7.0's API is native and `unstable/`, and projects may compile with 7, as the example
does), `magic-string` 1.4.3 and `@jridgewell/trace-mapping` 0.3.31. Vitest and
`@vitest/coverage-istanbul` 5.0.3 are peer dependencies.
