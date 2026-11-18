import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { RouteIdError } from "./query";
import { LEGACY_RULES, legacyTarget, matchPattern, toSearchParams, type LegacyRule } from "./legacy";

/**
 * The legacy redirect table. Each task that retires a URL adds its rows to LEGACY_RULES and its
 * cases here: [old pathname, search params, expected target].
 * SL.3 shipped the harness with no rules; SL.6, SL.7, SL.10 and SL.12 added theirs. SL.15 checks the
 * table is complete (below): every row has cases, every redirect page has a row, and every page
 * that existed before the milestone is either still a page or has a row.
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
  // SL.10: the Request Context page is the chat composer's popover; `context=open` opens it.
  ["/request-context", {}, "/agents"],
  ["/request-context", { agent: "a1" }, "/agents/a1/chat?context=open"],
  ["/request-context", { agent: "a1", version: "3" }, "/agents/a1/chat?version=3&context=open"],
  ["/request-context", { agent: "a1", version: "draft" }, "/agents/a1/chat?version=draft&context=open"],
  ["/request-context", { agent: "a 1/b", version: "0" }, "/agents/a%201%2Fb/chat?context=open"],
  ["/request-context", { agent: "..", version: "2" }, "/agents"],
  ["/request-context/", { agent: "a1", unrelated: "x" }, "/agents/a1/chat?context=open"],
  ["/settings/providers", {}, "/settings?tab=providers"],
  // SL.12: the run and trace pages are the trace Sheet on Traces.
  ["/traces/r1", {}, "/traces?trace=r1"],
  ["/traces/r1", { x: "1" }, "/traces?trace=r1"],
  ["/runs/r1", {}, "/traces?trace=r1&tab=events"],
  ["/runs/a%20b", {}, "/traces?trace=a+b&tab=events"],
  ["/traces/..", {}, null],
];

describe("legacyTarget: the table", () => {
  it.each(CASES)("%s %o → %s", (pathname, search, expected) => {
    expect(legacyTarget(pathname, search)).toBe(expected);
  });

  it("rows are grouped per task in plan order (SL.6, SL.7, SL.10, ST.1, SL.12)", () => {
    expect(LEGACY_RULES.map((rule) => [rule.task, rule.pattern])).toEqual([
      ["SL.6", "/agents/:id"],
      ["SL.6", "/playground"],
      ["SL.7", "/"],
      ["SL.10", "/request-context"],
      ["ST.1", "/settings/providers"],
      ["SL.12", "/traces/:runId"],
      ["SL.12", "/runs/:id"],
    ]);
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

/**
 * SL.15: the legacy table is complete. SL.8 (MCP), SL.9 (Processors), SL.11 (editor), SL.13 (Tools)
 * and SL.14 (Workflows) kept their URLs (the route map's "Same page" rows), so only SL.6, SL.7,
 * SL.10 and SL.12 retired any.
 */
describe("legacyTarget: the table is complete", () => {
  const APP = path.resolve(__dirname, "..", "..", "app");

  /** `app/agents/[id]/page.tsx` → `/agents/:id`. */
  const patternOf = (file: string) => {
    const dir = path.dirname(path.relative(APP, file)).split(path.sep).join("/");
    return dir === "." ? "/" : `/${dir.replace(/\[(\w+)\]/g, ":$1")}`;
  };

  const pages: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name === "page.tsx") pages.push(full);
    }
  };
  walk(APP);
  const redirectPages = pages.filter((file) => /\bredirect\(/.test(fs.readFileSync(file, "utf8"))).map(patternOf).sort();
  const livePages = pages.map(patternOf).filter((p) => !redirectPages.includes(p));
  const patterns = LEGACY_RULES.map((rule) => rule.pattern);

  it("every redirect page has a row, and every row has a redirect page", () => {
    // `/traces/:runId` uses the same param name as its folder; the run page's folder is `[id]`.
    expect(redirectPages).toEqual([...new Set(patterns)].sort());
  });

  it("every row has at least one case in CASES that resolves (and one that does not, for rows that can decline)", () => {
    for (const rule of LEGACY_RULES) {
      const own = CASES.filter(([pathname]) => matchPattern(rule.pattern, pathname));
      expect(own.some(([, , expected]) => expected !== null), `${rule.task} ${rule.pattern}`).toBe(true);
    }
  });

  /** The Studio pages before the milestone (milestone-rag-memory at 28d5ae1), as path patterns. */
  const BEFORE = [
    "/", "/agents", "/agents/:id", "/agents/new", "/logs", "/mcp", "/metrics", "/playground", "/processors",
    "/request-context", "/runs/:id", "/settings", "/tools", "/traces", "/traces/:runId", "/workflows",
    "/workflows/:id", "/workflows/:id/runs/:runId",
  ];

  it.each(BEFORE)("the pre-milestone page %s is still a page or has a legacy row", (pattern) => {
    const live = livePages.includes(pattern);
    const retired = patterns.includes(pattern);
    expect(live || retired, pattern).toBe(true);
    expect(live && retired, `${pattern} is both a live page and a legacy row`).toBe(false);
  });

  // The plan's "Old URLs" acceptance list: each lands on its new home with its state.
  it.each([
    ["/", {}, "/agents"],
    ["/", { range: "7d" }, "/metrics?range=7d"],
    ["/playground", { agent: "a1", conversation: "c1", model: "test:echo" }, "/agents/a1/chat/c1?model=test%3Aecho"],
    ["/request-context", { agent: "a1" }, "/agents/a1/chat?context=open"],
    ["/agents/a1", {}, "/agents/a1/chat"],
    ["/traces/r1", {}, "/traces?trace=r1"],
    ["/runs/r1", {}, "/traces?trace=r1&tab=events"],
  ] as const)("acceptance: %s %o → %s", (pathname, search, expected) => {
    expect(legacyTarget(pathname, search)).toBe(expected);
  });
});
