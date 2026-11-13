import fs from "node:fs";
import path from "node:path";
import React from "react";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { legacyTarget, RouteIdError, routes, type LegacyRule } from "./index";
import { collectQueries, collectRenderedIds, findDeadQueries, type SourceFile } from "@/test/testidInventory";

const nav = vi.hoisted(() => ({ search: "", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { ...original.api, agents: { ...original.api.agents, list: vi.fn(async () => [{ id: "a-1", name: "Support bot" }]) } } };
});
vi.mock("@/lib/traces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/traces")>()),
  listTraceRuns: vi.fn(),
}));
vi.mock("@/lib/workflows", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  listWorkflows: vi.fn(async () => [{ id: "wf-1", name: "Triage", draft_spec: null, latest_version: 3 }]),
}));
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(),
}));

// ---- property-style: ids of every shape round-trip through every builder ----

function rng(seed: number) {
  let s = seed >>> 0;
  return () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 2 ** 32);
}
const PIECES = ["a", "Z", "9", "é", "日本", "😀", "👩‍👩‍👧", " ", "/", "//", "%", "%2F", "%zz", ":", "?", "#", "&", "=", "+", "\\", "\"", "<", "\u0000", "\n"];
const FIXED = ["..", ".", "...", "", "a/b", "a%2Fb", "100%", " ", "x y", "😀", "日本語/テスト", ":", "a:b", "/", "?q=1#h", "a".repeat(5000), "é".repeat(2000)];
function randomIds(n: number): string[] {
  const r = rng(20261114);
  const ids = [...FIXED];
  for (let i = 0; i < n; i++) {
    const len = 1 + Math.floor(r() * 12);
    ids.push(Array.from({ length: len }, () => PIECES[Math.floor(r() * PIECES.length)]).join(""));
  }
  return ids;
}
const IDS = randomIds(300);
// Decided requirement (coordinator, SL.3 review, option b): an id that is exactly "." or ".." cannot be
// a path segment (URL parsers collapse it, %2E included), so path builders throw RouteIdError for it.
// The API refuses dot-only ids, so this only happens on a bug. Path round-trips use the other ids.
const DOT_IDS = [".", ".."];
const PATH_IDS = IDS.filter((x) => !DOT_IDS.includes(x));
const decodeSegs = (p: string) => p.split("?")[0].split("/").slice(1).map(decodeURIComponent);

describe("route builders: ids round-trip", () => {
  it("agent, trace, run and workflow builders keep the id in one segment that decodes back", () => {
    for (const id of PATH_IDS) {
      // SL.12: a trace is the Sheet over the list, `/traces?trace=<id>` (a run: `&tab=events`); the id
      // round-trips through its one query parameter. The retired page paths keep one path segment.
      for (const [href, tab] of [
        [routes.trace(id), null],
        [routes.run(id), "events"],
      ] as const) {
        const url = new URL(href, "http://studio.test");
        expect(url.pathname).toBe("/traces");
        expect(url.searchParams.getAll("trace")).toEqual([id]);
        expect(url.searchParams.get("tab")).toBe(tab);
        expect(url.hash).toBe("");
      }
      for (const [path, head] of [
        [routes.legacyTracePath(id), "traces"],
        [routes.legacyRunPath(id), "runs"],
        [routes.workflow(id), "workflows"],
      ] as const) {
        expect(path.startsWith(`/${head}/`)).toBe(true);
        expect(path.split("/")).toHaveLength(3);
        expect(decodeSegs(path)).toEqual([head, id]);
      }
      // The agent page's tabs (SL.6): the id is the second of three segments.
      for (const [path, tab] of [
        [routes.agentEditor(id), "editor"],
        [routes.agentChat(id), "chat"],
        [routes.agentTraces(id), "traces"],
      ] as const) {
        expect(decodeSegs(path)).toEqual(["agents", id, tab]);
      }
    }
  });

  it("workflowRun keeps both ids apart", () => {
    for (let i = 0; i < PATH_IDS.length - 1; i++) {
      const p = routes.workflowRun(PATH_IDS[i], PATH_IDS[i + 1]);
      expect(p.split("/")).toHaveLength(5);
      expect(decodeSegs(p)).toEqual(["workflows", PATH_IDS[i], "runs", PATH_IDS[i + 1]]);
    }
  });

  it("an id never leaks a query or fragment delimiter into the path", () => {
    for (const id of PATH_IDS) {
      // SL.12: the trace id lives in the query, so the check is on the retired page path, plus no
      // fragment and no extra parameter from the trace builder.
      const url = new URL(routes.legacyTracePath(id), "http://studio.test");
      expect(url.search).toBe("");
      expect(url.hash).toBe("");
      const sheet = new URL(routes.trace(id), "http://studio.test");
      expect(sheet.hash).toBe("");
      expect([...sheet.searchParams.keys()]).toEqual(["trace"]);
    }
  });

  it("resolved by a browser URL parser, the id stays one segment (dot segments must not collapse)", () => {
    // "." and "..": RouteIdError instead of a URL that resolves elsewhere (decided requirement above).
    for (const id of DOT_IDS) {
      for (const build of [routes.agentEditor, routes.trace, routes.run, routes.legacyTracePath, routes.legacyRunPath, routes.workflow]) {
        expect(() => build(id)).toThrow(RouteIdError);
        expect(() => build(id)).toThrow("route id cannot be '.' or '..'");
      }
      expect(() => routes.workflowRun("wf-1", id)).toThrow(RouteIdError);
      expect(() => routes.workflowRun(id, "r1")).toThrow(RouteIdError);
    }
    for (const id of PATH_IDS.filter((x) => x !== "")) {
      const url = new URL(routes.agentEditor(id), "http://studio.test");
      expect(url.pathname.split("/")).toHaveLength(4);
      expect(decodeURIComponent(url.pathname.split("/")[2])).toBe(id);
    }
  });
});

describe("route builders: queries round-trip", () => {
  it("playground options come back through URLSearchParams unchanged, in fixed key order", () => {
    for (let i = 0; i < IDS.length - 3; i++) {
      const [agent, conversation, model, context] = [IDS[i], IDS[i + 1], IDS[i + 2], IDS[i + 3]];
      const href = routes.playground({ agent, conversation, version: "draft", model, context });
      const q = new URL(href, "http://x").searchParams;
      const want = { agent, conversation, version: "draft", model, context };
      for (const [k, v] of Object.entries(want)) {
        if (v === "") expect(q.has(k)).toBe(false);
        else expect(q.get(k)).toBe(v);
      }
      expect([...q.keys()]).toEqual(Object.keys(want).filter((k) => want[k as keyof typeof want] !== ""));
    }
  });

  it("record queries round-trip for every value shape, dropping null/undefined/false/empty and keeping 0/true", () => {
    for (const v of IDS.filter((x) => x !== "")) {
      const href = routes.traces({ q: v, zero: 0, on: true, off: false, none: null, gone: undefined, blank: "" });
      const q = new URL(href, "http://x").searchParams;
      expect(q.get("q")).toBe(v);
      expect(q.get("zero")).toBe("0");
      expect(q.get("on")).toBe("true");
      for (const k of ["off", "none", "gone", "blank"]) expect(q.has(k)).toBe(false);
    }
  });

  it("URLSearchParams (repeated keys included) pass through every query-taking builder", () => {
    const sp = new URLSearchParams();
    sp.append("status", "failed,needs_attention");
    sp.append("tag", "a&b");
    sp.append("tag", "😀 =");
    for (const build of [routes.metrics, routes.traces, routes.logs]) {
      const q = new URL(build(sp), "http://x").searchParams;
      expect(q.get("status")).toBe("failed,needs_attention");
      expect(q.getAll("tag")).toEqual(["a&b", "😀 ="]);
    }
  });

  it("an empty query leaves the bare path", () => {
    expect(routes.logs({})).toBe("/logs");
    expect(routes.logs(new URLSearchParams())).toBe("/logs");
    expect(routes.traces({ a: null, b: false, c: "" })).toBe("/traces");
    expect(routes.playground()).toBe("/playground");
  });
});

// ---- legacy harness with a test rules table ----

const RULES: LegacyRule[] = [
  { task: "T", pattern: "/old/agents/:id", to: (p, s) => `/new/agents/${encodeURIComponent(p.id)}${s.size ? `?${s}` : ""}` },
  { task: "T", pattern: "/old/runs/:run/step/:n", to: (p) => `/new/${encodeURIComponent(p.run)}/${encodeURIComponent(p.n)}` },
  { task: "T", pattern: "/go", to: (_p, s) => `/${s.get("next") ?? ""}` },
  { task: "T", pattern: "/jump/:to", to: (p) => `/dest/${encodeURIComponent(p.to)}` },
];

describe("legacyTarget: test rules table", () => {
  it("fills params from the path and keeps the query", () => {
    expect(legacyTarget("/old/agents/a1", { tab: "x", q: "a b" }, RULES)).toBe("/new/agents/a1?tab=x&q=a+b");
    expect(legacyTarget("/old/runs/r%2F1/step/3", {}, RULES)).toBe("/new/r%2F1/3");
  });

  it("returns null for non-matching paths", () => {
    for (const p of ["/", "/old", "/old/agents", "/old/agents/a/b", "/new/agents/a1", "/OLD/agents/a1", "/old/runs/r/step"]) {
      expect(legacyTarget(p, {}, RULES)).toBeNull();
    }
  });

  it("accepts array-valued searchParams and keeps every value, in order", () => {
    const out = legacyTarget("/old/agents/a1", { tag: ["x", "y z"], one: "1", skip: undefined }, RULES)!;
    expect(new URL(out, "http://h").searchParams.getAll("tag")).toEqual(["x", "y z"]);
    expect(out).not.toContain("skip");
    expect(legacyTarget("/old/agents/a1", new URLSearchParams("t=1&t=2"), RULES)).toBe("/new/agents/a1?t=1&t=2");
  });

  it("does not mutate a URLSearchParams input", () => {
    const sp = new URLSearchParams("a=1");
    legacyTarget("/old/agents/a1", sp, RULES);
    expect(sp.toString()).toBe("a=1");
  });
});

describe("legacyTarget: no open redirect", () => {
  const CRAFTED = ["//evil.com", "https://evil.com", "%2F%2Fevil.com", "\\\\evil.com", "/\\evil.com", "javascript:alert(1)", "@evil.com", "evil.com", "\t//evil.com"];
  const safe = (t: string) => t.startsWith("/") && !t.startsWith("//") && !t.startsWith("/\\");

  it("a rule that encodes its params never yields an absolute or protocol-relative target", () => {
    for (const evil of CRAFTED) {
      for (const pathname of [`/jump/${encodeURIComponent(evil)}`, `/old/agents/${encodeURIComponent(evil)}`]) {
        const t = legacyTarget(pathname, { next: evil, to: evil }, RULES);
        expect(t, pathname).not.toBeNull();
        expect(safe(t!), `${pathname} -> ${t}`).toBe(true);
        expect(new URL(t!, "http://studio.test").origin).toBe("http://studio.test");
      }
    }
  });

  it("a crafted request path cannot match a rule through a leading // or extra segments", () => {
    for (const p of ["//evil.com/jump/x", "//jump/x", "/jump//evil.com", "/jump/a/b", "https://evil.com/jump/x"]) {
      const t = legacyTarget(p, {}, RULES);
      if (t !== null) expect(safe(t), `${p} -> ${t}`).toBe(true);
    }
  });

  it("the harness refuses a rule that would emit a protocol-relative or absolute URL from a query param", () => {
    for (const evil of ["/evil.com", "//evil.com", "https://evil.com"]) {
      // rule "/go" builds `/${next}`; next="/evil.com" would give "//evil.com"
      const t = legacyTarget("/go", { next: evil }, RULES);
      if (t !== null) expect(safe(t), `next=${evil} -> ${t}`).toBe(true);
    }
  });
});

// ---- static scan: no raw internal route strings outside lib/routes ----

const srcDir = path.resolve(__dirname, "../..");
function walk(dir: string, out: string[] = []): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) walk(full, out);
    else if (/\.tsx?$/.test(e.name) && !/\.test\.tsx?$/.test(e.name)) out.push(full);
  }
  return out;
}
const routesDir = path.join(srcDir, "lib", "routes");
const scanned = [
  ...walk(path.join(srcDir, "app")),
  ...walk(path.join(srcDir, "components")),
  ...walk(path.join(srcDir, "lib")).filter((f) => !f.startsWith(routesDir + path.sep)),
];
// Route names come from what the builders emit (first path segment), not a hand-kept list, so a new
// area is scanned as soon as it has a builder.
const ROUTE_NAMES = [
  ...new Set(
    Object.values(routes as unknown as Record<string, (...a: unknown[]) => string>)
      .map((build) => build("x", "y"))
      .map((url) => url.split(/[/?]/)[1])
      .filter((name): name is string => !!name),
  ),
].sort((a, b) => b.length - a.length);
const NAMES = ROUTE_NAMES.map((n) => n.replace(/[-]/g, "\\-")).join("|");
// API client calls take paths relative to /api/v1 (`request("/processors")`); those are not page links.
const API_CALL = /\b(?:request|getJson|postJson|fetch|api\.\w+)\s*(?:<[^>]*>)?\(/;
// An internal route literal: starts with "/" then a letter, "?", or is just "/" ; API paths (/api/) are fine.
const ROUTE = String.raw`\/(?!api\/)(?:[A-Za-z?][^"'\`]*)?`;
const PATTERNS: Array<[string, RegExp]> = [
  ["href=\"/…\"", new RegExp(String.raw`href=\s*(?:\{\s*)?["'\`]${ROUTE}["'\`]`)],
  ["href: \"/…\" key", new RegExp(String.raw`\bhref\s*:\s*["'\`]${ROUTE}["'\`]`)],
  ["href: `/…${}` key", /\bhref\s*:\s*`\/(?!api\/)/],
  ["push/replace/prefetch(\"/…\")", new RegExp(String.raw`\.(?:push|replace|prefetch)\(\s*["'\`]${ROUTE}["'\`]`)],
  ["href={`/…`} template", /href=\{\s*`\/(?!api\/)/],
  ["push/replace(`/…${}`)", /\.(?:push|replace)\(\s*`\/(?!api\/)/],
  ["ternary/const/return route literal", new RegExp(String.raw`(?:\?|:|=|\(|,|\breturn)\s*[\`"']\/(?:${NAMES})(?:[/?\`"'])`)],
];

describe("no raw internal route strings in src/app, src/components and src/lib (outside lib/routes)", () => {
  it("scans a meaningful number of files", () => {
    expect(scanned.length).toBeGreaterThan(80);
    expect(scanned.some((f) => f.endsWith(path.join("lib", "nav.ts")))).toBe(true);
    expect(scanned.some((f) => f.startsWith(routesDir))).toBe(false);
  });

  it("derives the route names from the builders", () => {
    for (const name of ["agents", "playground", "request-context", "workflows", "traces", "runs", "logs", "metrics", "tools", "mcp", "processors", "settings"]) {
      expect(ROUTE_NAMES).toContain(name);
    }
  });

  it("the patterns catch what they should (scanner self-check)", () => {
    const hits = (s: string) => !API_CALL.test(s) && PATTERNS.some(([, re]) => re.test(s));
    for (const bad of ['<Link href="/traces">', "<a href='/'>", "<a href={`/runs/${id}`}>", 'router.push("/agents")', "router.replace(`/logs?${q}`)", "x ? '/traces' : y",
      '{ label: "Agents", href: "/agents" }', "{ href: `/runs/${id}` }", "return `/metrics?${qs}`;", 'return "/request-context";']) {
      expect(hits(bad), bad).toBe(true);
    }
    for (const ok of ['fetch("/api/v1/runs")', "<a href={routes.trace(id)}>", 'href="#main"', 'href="https://x.dev"', "router.push(routes.agents())", 'className="a/b"',
      'request<ProcessorCatalog>("/processors")', "{ href: routes.trace(id) }"]) {
      expect(hits(ok), ok).toBe(false);
    }
  });

  it("no href, router.push/replace or template route literal remains", () => {
    const found: string[] = [];
    for (const file of scanned) {
      const lines = fs.readFileSync(file, "utf-8").split("\n");
      lines.forEach((line, i) => {
        if (/^\s*(\/\/|\*|\/\*)/.test(line) || API_CALL.test(line)) return;
        for (const [name, re] of PATTERNS) if (re.test(line)) found.push(`${path.relative(srcDir, file)}:${i + 1} [${name}] ${line.trim()}`);
      });
    }
    expect(found).toEqual([]);
  });

  it("tracePath and workflowRunPath helpers are gone", () => {
    const all = walk(srcDir).map((f) => fs.readFileSync(f, "utf-8")).join("\n");
    expect(all).not.toMatch(/\b(?:tracePath|workflowRunPath)\b/);
  });
});

// ---- test-id inventory: dead id fails, then passes once rendered ----

describe("test-id inventory: a dead id fails until a source renders it", () => {
  const test: SourceFile = { path: "x.test.tsx", text: 'screen.getByTestId("sl3-new-widget");' };
  const queries = collectQueries([test]);

  it("fails against sources without the id", () => {
    const rendered = collectRenderedIds([{ path: "A.tsx", text: '<div data-testid="other" />' }]);
    const dead = findDeadQueries(queries, rendered, []);
    expect(dead.map((d) => d.value)).toEqual(["sl3-new-widget"]);
    expect(dead[0].reason).toMatch(/no source file renders/);
  });

  it("passes once a source renders it", () => {
    const rendered = collectRenderedIds([
      { path: "A.tsx", text: '<div data-testid="other" />' },
      { path: "B.tsx", text: '<span data-testid="sl3-new-widget" />' },
    ]);
    expect(findDeadQueries(queries, rendered, [])).toEqual([]);
  });

  it("a dynamic render needs its prefix allowlisted to count", () => {
    const rendered = collectRenderedIds([{ path: "B.tsx", text: "<span data-testid={`sl3-new-${id}`} />" }]);
    expect(findDeadQueries(queries, rendered, [])).toHaveLength(1);
    expect(findDeadQueries(queries, rendered, ["sl3-new-"])).toEqual([]);
  });
});

// ---- behaviour unchanged: rendered hrefs equal the old literals (written out, not via routes.*) ----

const RUN = "11111111-2222-3333-4444-555555555555";
const baseRun = {
  run_id: "run-1", kind: "agent" as const, agent_id: "a-1", version: 1, tool_id: null, workflow_id: null, workflow_version: null,
  status: "completed", display_status: "completed", input: "in", output: "out", error: null, heartbeat_at: null,
  created_at: "2026-11-12T10:00:00Z", updated_at: "2026-11-12T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
};

describe("rendered links are unchanged for ordinary ids", () => {
  beforeEach(() => {
    nav.search = "";
    nav.push.mockReset();
    nav.replace.mockReset();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("metrics overview (the former dashboard): empty-state links, failure row and view-all, range replace", async () => {
    const { breakdownFixture, summaryFixture, timeseriesFixture } = await import("@/components/metrics/metricsFixtures");
    const { default: MetricsPage } = await import("@/app/metrics/page");
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    const failed = { ...baseRun, run_id: "run-failed-1", status: "failed", display_status: "failed", error: "boom" };
    let runs: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: string) => {
      const u = new URL(input);
      let body: unknown = { items: [], next_before: null };
      if (u.pathname === "/api/v1/runs") body = { items: runs, next_before: null };
      else if (u.pathname.endsWith("/summary")) body = summaryFixture(runs.length ? { runs: 3, failed: 1 } : {}, {});
      else if (u.pathname.endsWith("/timeseries")) body = timeseriesFixture([]);
      else if (u.pathname.endsWith("/breakdown")) body = breakdownFixture("agent", []);
      else if (u.pathname === "/api/v1/workflows") body = [];
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const { listTraceRuns } = await import("@/lib/traces");
    vi.mocked(listTraceRuns).mockImplementation(async () => ({ items: runs, next_before: null }) as never);
    const first = render(React.createElement(MetricsPage));
    const empty = await screen.findByTestId("metrics-empty");
    expect(within(empty).getByRole("link", { name: "Agents" })).toHaveAttribute("href", "/agents");
    expect(within(empty).getByRole("link", { name: "Workflows" })).toHaveAttribute("href", "/workflows");
    first.unmount();
    runs = [failed];
    nav.search = "range=30d";
    render(React.createElement(MetricsPage));
    await waitFor(() => expect(screen.getByTestId("recent-failure")).toHaveAttribute("href", "/traces?trace=run-failed-1"));
    expect(screen.getByTestId("recent-failures-all")).toHaveAttribute("href", expect.stringMatching(/^\/traces\?status=failed%2Cneeds_attention/));
    fireEvent.change(screen.getByLabelText("Date range"), { target: { value: "24h" } });
    expect(nav.replace).toHaveBeenCalledWith("/metrics?range=24h", { scroll: false });
    fireEvent.change(screen.getByLabelText("Date range"), { target: { value: "7d" } });
    expect(nav.replace).toHaveBeenLastCalledWith("/metrics", { scroll: false });
  });

  it("traces list: row link, workflow run link, filter replace", async () => {
    const { listTraceRuns } = await import("@/lib/traces");
    const { default: TracesPage } = await import("@/app/traces/page");
    const wf = { ...baseRun, run_id: "wfrun-1", kind: "workflow" as const, agent_id: null, version: null, workflow_id: "wf-1", workflow_version: 3 };
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [baseRun, wf], next_before: null } as never);
    render(React.createElement(TracesPage));
    const rows = await screen.findAllByTestId("trace-row");
    expect(rows[0].querySelector("[data-testid='trace-row-open']")).toHaveAttribute("href", "/traces?trace=run-1");
    expect(await screen.findByRole("link", { name: "Workflow: Triage v3" })).toHaveAttribute("href", "/workflows/wf-1/runs/wfrun-1");
    fireEvent.change(screen.getByLabelText(/filter by status/i), { target: { value: "failed" } });
    expect(nav.replace).toHaveBeenLastCalledWith("/traces?status=failed");
  });

  it("logs: the trace link per record and the filter replace", async () => {
    const { searchLogs } = await import("@/components/logs/logsQuery");
    const { default: LogsPage } = await import("@/app/logs/page");
    const item = { ts: "2026-11-13T10:00:00.123456+00:00", level: "error", message: "model call failed", service: "eap-worker", trace_id: "t1", span_id: "s1", run_id: RUN };
    vi.mocked(searchLogs).mockResolvedValue({ items: [item], from: "2026-11-13T09:00:00+00:00", to: "2026-11-13T10:00:00+00:00", limit: 100 } as never);
    render(React.createElement(LogsPage));
    expect(await screen.findByRole("link", { name: `Open trace of run ${RUN}` })).toHaveAttribute("href", `/traces?trace=${RUN}`);
  });

  it("playground: the turn footer links to the run's trace", async () => {
    const { TurnFooter } = await import("@/components/chat/TurnFooter");
    render(React.createElement(TurnFooter, {
      turn: {
        runId: "run-9", status: "completed", modelId: "m", modelOverridden: false, draft: false, version: 1,
        usage: { recorded: true, inputTokens: 1, outputTokens: 1, costUsd: 0.1, measured: true },
        checking: false, block: null, liveText: true, announce: false,
      } as never,
    }));
    expect(screen.getByRole("link", { name: "Open trace" })).toHaveAttribute("href", "/traces?trace=run-9");
    expect(routes.playground({ agent: "a1", conversation: "c1" })).toBe("/playground?agent=a1&conversation=c1");
  });
});
