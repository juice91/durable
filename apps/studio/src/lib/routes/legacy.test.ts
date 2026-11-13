import { describe, expect, it } from "vitest";
import { LEGACY_RULES, legacyTarget, matchPattern, toSearchParams, type LegacyRule } from "./legacy";

/**
 * The legacy redirect table. Each task that retires a URL adds its rows to LEGACY_RULES and its
 * cases here: [old pathname, search params, expected target].
 * SL.3 ships the harness with no rules, so every URL is current.
 */
const CASES: Array<[string, Record<string, string | string[] | undefined>, string | null]> = [
  // SL.6, SL.7, SL.10, SL.12 add their rows here (see the plan's route map).
];

describe("legacyTarget: the table", () => {
  it.each(CASES)("%s %o → %s", (pathname, search, expected) => {
    expect(legacyTarget(pathname, search)).toBe(expected);
  });

  it("has no rules yet: today's URLs are all current", () => {
    expect(LEGACY_RULES).toHaveLength(0);
    for (const url of ["/", "/agents/a1", "/playground", "/request-context", "/traces/r1", "/runs/r1"]) {
      expect(legacyTarget(url, {})).toBeNull();
    }
  });
});

describe("legacyTarget: the harness", () => {
  const rules: LegacyRule[] = [
    { task: "test", pattern: "/old/:id", to: (p, s) => `/new/${encodeURIComponent(p.id)}${s.toString() ? `?${s}` : ""}` },
    { task: "test", pattern: "/maybe", to: (_p, s) => (s.get("go") ? "/went" : null) },
    { task: "test", pattern: "/maybe", to: () => "/fallback" },
  ];

  it("matches :params, decodes them, and passes the search params", () => {
    expect(legacyTarget("/old/a%3Ab", { x: "1" }, rules)).toBe("/new/a%3Ab?x=1");
    expect(legacyTarget("/old/a1/", undefined, rules)).toBe("/new/a1");
  });

  it("returns null for no match, an extra segment or an empty segment", () => {
    expect(legacyTarget("/other", {}, rules)).toBeNull();
    expect(legacyTarget("/old/a/b", {}, rules)).toBeNull();
    expect(legacyTarget("/old/", {}, rules)).toBeNull();
  });

  it("falls through to the next rule when a rule returns null", () => {
    expect(legacyTarget("/maybe", { go: "1" }, rules)).toBe("/went");
    expect(legacyTarget("/maybe", {}, rules)).toBe("/fallback");
  });

  it("accepts Next 15 searchParams records (array values) and URLSearchParams", () => {
    expect(toSearchParams({ a: ["1", "2"], b: "x", c: undefined }).toString()).toBe("a=1&a=2&b=x");
    expect(toSearchParams(new URLSearchParams("a=1")).toString()).toBe("a=1");
    expect(toSearchParams(null).toString()).toBe("");
  });

  it("drops a target that would leave the origin", () => {
    for (const bad of ["//evil.com", "/\\evil.com", "https://evil.com", "evil.com", "/\t/evil.com", "javascript:alert(1)"]) {
      expect(legacyTarget("/x", {}, [{ task: "t", pattern: "/x", to: () => bad }]), bad).toBeNull();
    }
    expect(legacyTarget("/x", {}, [{ task: "t", pattern: "/x", to: () => "/ok?next=//evil.com" }])).toBe("/ok?next=//evil.com");
  });

  it("rejects a malformed percent-encoding instead of throwing", () => {
    expect(matchPattern("/old/:id", "/old/%E0%A4%A")).toBeNull();
  });
});
