import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { validateEmail } from "../src/email";

describe("validateEmail", () => {
  it("returns the domain", () => {
    expect(validateEmail("bob@acme.com")).toBe("acme.com");
  });

  it("rejects a string without an @", () => {
    expect(() => validateEmail("bob.acme.com")).toThrow("not an email address");
  });

  it("rejects a domain without a dot", () => {
    expect(() => validateEmail("bob@localhost")).toThrow("unsupported domain");
  });

  // Instrumented (Istanbul, coverset's plugin) or not, a stack frame names the line in the source.
  it("reports the line it throws on", () => {
    const source = readFileSync(new URL("../src/email.ts", import.meta.url), "utf8").split("\n");
    const line = source.findIndex((l) => l.includes('throw new Error("not an email address")')) + 1;
    let stack = "";
    try {
      validateEmail("bob.acme.com");
    } catch (error) {
      stack = (error as Error).stack ?? "";
    }
    const frame = stack.split("\n").find((l) => l.includes("src/email.ts:")) ?? stack;
    expect(frame).toMatch(new RegExp(`src/email\\.ts:${line}:\\d+\\)?$`));
  });
});
