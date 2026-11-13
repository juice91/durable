import { describe, expect, it } from "vitest";
import { RouteIdError } from "./query";
import { LEGACY_RULES, legacyTarget, matchPattern, toSearchParams, type LegacyRule } from "./legacy";

/**
 * The legacy redirect table. Each task that retires a URL adds its rows to LEGACY_RULES and its
 * cases here: [old pathname, search params, expected target].
 * SL.3 ships the harness with no rules, so every URL is current.
 */
const CASES: Array<[string, Record<string, string | string[] | undefined>, string | null]> = [
  // SL.6: the agent page.
  ["/agents/a1", {}, "/agents/a1/chat"],
  ["/agents/a1/", {}, "/agents/a1/chat"],
  ["/agents/ns%3Aagent%2F1", {}, "/agents/ns%3Aagent%2F1/chat"],
  ["/agents/new", {}, null],
  ["/agents", {}, null],
  ["/agents/a1/chat", {}, null],
  ["/agents/a1/editor", {}, null],
  // SL.6: the playground, every parameter combination.
  ["/playground", {}, null],
  ["/playground", { conversation: "c1" }, null],
  ["/playground", { version: "3", model: "openai:gpt-4.1" }, null],
  ["/playground", { agent: "a1" }, "/agents/a1/chat"],
  ["/playground", { agent: "a1", conversation: "c1" }, "/agents/a1/chat/c1"],
  ["/playground", { agent: "a1", version: "3" }, "/agents/a1/chat?version=3"],
  ["/playground", { agent: "a1", version: "draft" }, "/agents/a1/chat?version=draft"],
  ["/playground", { agent: "a1", version: "latest" }, "/agents/a1/chat"],
  ["/playground", { agent: "a1", version: "0" }, "/agents/a1/chat"],
  ["/playground", { agent: "a1", model: "openai:gpt-4.1" }, "/agents/a1/chat?model=openai%3Agpt-4.1"],
  ["/playground", { agent: "a1", context: "preset" }, "/agents/a1/chat?context=preset"],
  [
    "/playground",
    { agent: "a1", conversation: "c1", version: "2", model: "test:echo", context: "preset" },
    "/agents/a1/chat/c1?version=2&model=test%3Aecho&context=preset",
  ],
  ["/playground", { context: "preset", agent: "a 1", conversation: "c/1" }, "/agents/a%201/chat/c%2F1?context=preset"],
  ["/playground", { agent: "a1", unknown: "x" }, "/agents/a1/chat"],
  // Dot-only ids cannot be a path segment (RouteIdError): the agents list, never a thrown error.
  ["/playground", { agent: ".." }, "/agents"],
  ["/playground", { agent: "." }, "/agents"],
  ["/playground", { agent: "a1", conversation: ".." }, "/agents"],
  ["/agents/..", {}, "/agents"],
  // SL.7: the dashboard folded into Metrics; a bare `/` opens Agents.
  ["/", {}, "/agents"],
  ["/", { range: "30d" }, "/metrics?range=30d"],
  ["/", { range: "custom", from: "2026-11-01T00:00:00Z", to: "2026-11-05T00:00:00Z" }, "/metrics?range=custom&from=2026-11-01T00%3A00%3A00Z&to=2026-11-05T00%3A00%3A00Z"],
  ["/", { from: "2026-11-01T00:00:00Z", to: "2026-11-05T00:00:00Z" }, "/metrics?from=2026-11-01T00%3A00%3A00Z&to=2026-11-05T00%3A00%3A00Z"],
  // The dashboard ignored `interval` (and the filters), so they are not carried.
  ["/", { interval: "hour" }, "/agents"],
  ["/", { range: "24h", interval: "hour" }, "/metrics?range=24h"],
  ["/", { range: "7d", agent_id: "a1", utm: "x" }, "/metrics?range=7d"],
  ["/", { agent_id: "a1" }, "/agents"],
  ["/", { range: ["24h", "30d"] }, "/metrics?range=24h&range=30d"],
  ["/", { range: "" }, "/metrics?range="],
  ["/", { range: "//evil.com" }, "/metrics?range=%2F%2Fevil.com"],
  // SL.10, SL.12 add their rows here (see the plan's route map).
];

describe("legacyTarget: the table", () => {
  it.each(CASES)("%s %o → %s", (pathname, search, expected) => {
    expect(legacyTarget(pathname, search)).toBe(expected);
  });

  it("rows are grouped per task in plan order (SL.6, SL.7); URLs no task has retired yet are current", () => {
    expect(LEGACY_RULES.map((rule) => [rule.task, rule.pattern])).toEqual([
      ["SL.6", "/agents/:id"],
      ["SL.6", "/playground"],
      ["SL.7", "/"],
    ]);
    for (const url of ["/request-context", "/traces/r1", "/runs/r1"]) {
      expect(legacyTarget(url, {})).toBeNull();
    }
  });
});

describe("legacyTarget: the harness", () => {
  it("treats a rule that throws RouteIdError as no target, and rethrows anything else", () => {
    const throwing: LegacyRule[] = [
      { task: "t", pattern: "/x", to: () => { throw new RouteIdError(".."); } },
      { task: "t", pattern: "/x", to: () => "/next" },
    ];
    expect(legacyTarget("/x", {}, throwing)).toBe("/next");
    expect(() => legacyTarget("/y", {}, [{ task: "t", pattern: "/y", to: () => { throw new Error("bug"); } }])).toThrow("bug");
  });

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
