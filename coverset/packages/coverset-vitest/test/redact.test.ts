/**
 * Redaction, ported from `packages/pytest-coverset/tests/test_features.py`: the same names, patterns
 * and limits, with the reporter's JavaScript-flavoured reprs (`"abc"` where Python shows `'abc'`).
 */
import { describe, expect, it } from "vitest";

import { exampleRepr, redactMessage, scrub } from "../src/redact.js";

describe("exampleRepr", () => {
  it("redacts secret-named parameters and scrubs credentials and email local parts", () => {
    expect(exampleRepr("password", "hunter2hunter2")).toBe("<redacted:len=14>");
    expect(exampleRepr("api_key", 12345)).toBe("<redacted>");
    expect(exampleRepr("email", "bob@sub.example.com")).toBe('"b***@sub.example.com"');
    expect(exampleRepr("note", "token ghp_" + "a".repeat(36))).toBe('"token ghp_[REDACTED]"');
    expect(exampleRepr("values", [1, 2])).toBe("[1, 2]");
    expect((exampleRepr("s", "x".repeat(500)) ?? "").length).toBeLessThanOrEqual(50);
    expect(scrub("Authorization: Bearer abc.def")).toBe("Authorization: Bearer [REDACTED]");
  });

  it("scrubs secrets before shortening the text", () => {
    const token = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop";
    expect(exampleRepr("note", token + " end")).toBe('"sk-ant-[REDACTED] end"');
    const email = exampleRepr("note", "please email johnsmith.private@corp.example.com about it") ?? "";
    expect(email).not.toContain("johnsmit");
    expect(email).toContain("j***@corp.example.com");
    const bearer = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEF tail";
    expect(exampleRepr("header", bearer)).toBe('"Authorization: Bearer [REDACTED] tail"');
    const longTail = "x".repeat(100) + " Authorization: Bearer " + "q".repeat(60) + " ok";
    expect(exampleRepr("note", longTail)).not.toContain("qqq");
    const pemInHuge = "-----BEGIN RSA PRIVATE KEY-----\n" + "K".repeat(20_000);
    expect(exampleRepr("note", pemInHuge)).not.toContain("KKK");
    for (const value of [token, bearer, longTail]) expect(exampleRepr("v", [value, value])).not.toContain("ABCDEF");
  });

  it("redacts the return values of secret-named functions", () => {
    const secret = "ghx_supersecretvalue1234567890";
    expect(exampleRepr("return", secret, { funcName: "make_reset_token" })).toBe("<redacted:len=30>");
    expect(exampleRepr("return", 7, { funcName: "getApiKey" })).toBe("<redacted>");
    expect(exampleRepr("return", "abc", { funcName: "normalise" })).toBe('"abc"');
    expect(exampleRepr("text", "abc", { funcName: "makeToken" })).toBe('"abc"');
  });

  it("redacts secret keys inside object examples", () => {
    const kwargs = { password: "hunter2", api_key: "abc", name: "bob", nested: { session_id: [1, 2] } };
    expect(exampleRepr("opts", kwargs)).toBe(
      '{"password": <redacted:len=7>, "api_key": <redacted:len=3>, "name": "bob", "nested": {"session_id": <redacted:len=2>}}',
    );
    expect(exampleRepr("opts", { token: 5 })).toBe('{"token": <redacted>}');
    expect(exampleRepr("credentials", "sekrit")).toBe("<redacted:len=6>");
    expect(exampleRepr("db_creds", "sekrit")).toBe("<redacted:len=6>");
    expect(exampleRepr("opts", new Map([["cookie", "c=1"]]))).toBe('Map {"cookie" => <redacted:len=3>}');
  });

  it("shortens like reprlib and summarises huge integers", () => {
    expect(exampleRepr("n", 10n ** 5000n)).toBe("<int 16610 bits>");
    expect(exampleRepr("n", -(2n ** 200n))).toBe("<int 201 bits>");
    expect(exampleRepr("n", 2n ** 100n)).toBe(String(2n ** 100n));
    expect(exampleRepr("xs", Array.from({ length: 10 }, (_, i) => i))).toBe("[0, 1, 2, 3, 4, 5, ...]");
    expect(exampleRepr("deep", [[[1]]])).toBe("[[[...]]]");
    expect(exampleRepr("big", { a: 1, b: 2, c: 3, d: 4, e: 5 })).toBe('{"a": 1, "b": 2, "c": 3, "d": 4, ...}');
    const shown = exampleRepr("s", "a".repeat(30) + "b".repeat(30)) ?? "";
    expect(shown).toBe('"' + "a".repeat(21) + "..." + "b".repeat(22) + '"');
    expect(shown.length).toBe(48);
  });

  it("names enum members and leaves class instances out at the top level", () => {
    const enumSpec = { name: "Currency", members: { USD: "USD", EUR: "EUR" } };
    expect(exampleRepr("currency", "EUR", { enumSpec })).toBe("Currency.EUR");
    expect(exampleRepr("currency", "JPY", { enumSpec })).toBe('"JPY"');
    class Money {}
    expect(exampleRepr("m", new Money())).toBeNull();
    expect(exampleRepr("ms", [new Money()])).toBe("[<Money>]");
    expect(exampleRepr("f", () => 1)).toBeNull();
  });

  it("never emits a lone surrogate", () => {
    const shown = exampleRepr("s", "\ud83d" + "x".repeat(100) + "\ude00") ?? "";
    expect(shown.isWellFormed()).toBe(true);
    expect(redactMessage("bad \ud800 text").isWellFormed()).toBe(true);
  });
});

describe("redactMessage", () => {
  it("scrubs and cuts at 8 KiB", () => {
    expect(redactMessage("token ghp_" + "a".repeat(40))).toBe("token ghp_[REDACTED]");
    expect(redactMessage("x".repeat(10_000)).length).toBe(8192);
  });
});

describe("scrub", () => {
  it("applies the control plane's patterns, and bare secret names", () => {
    expect(scrub("DB_PASSWORD=hunter2 next")).toBe("DB_PASSWORD=[REDACTED] next");
    expect(scrub("password=hunter2&user=bob")).toBe("password=[REDACTED]&user=bob");
    expect(scrub('{"password": "hunter2", "user": "bob"}')).toBe('{"password": "[REDACTED]", "user": "bob"}');
    expect(scrub("'api_key': 'abc123'")).toBe("'api_key': '[REDACTED]'");
    expect(scrub("https://bob:hunter2@example.com/x")).toBe("https://[REDACTED]@example.com/x");
    expect(scrub("https://x-access-token:ghs_abc@github.com")).toBe("https://x-access-token:[REDACTED]@github.com");
    expect(scrub("Authorization: Basic dXNlcjpwYXNz")).toBe("Authorization: Basic [REDACTED]");
    expect(scrub('"authorization": "Bearer abc.def"')).toBe('"authorization": "Bearer [REDACTED]"');
    expect(scrub("GH_TOKEN_ghs_" + "a".repeat(30))).toBe("GH_TOKEN_ghs_[REDACTED]");
    expect(scrub("ak-" + "A".repeat(24))).toBe("ak-[REDACTED]");
    expect(scrub("monkey=1 tokens=2 page=3")).toBe("monkey=1 tokens=2 page=3");
  });

  it("scrubs failure messages with the same patterns", () => {
    const message = "login failed for password=hunter2 Authorization: Bearer abc url https://bob:pw@example.com";
    expect(redactMessage(message)).toBe("login failed for password=[REDACTED] Authorization: Bearer [REDACTED] url https://[REDACTED]@example.com");
  });
});
