import { readFileSync } from "node:fs";

export const PRODUCER = "coverset-vitest";

/** This package's version, from its `package.json`. */
export const VERSION: string = (
  JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as { version: string }
).version;
