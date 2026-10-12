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
});
