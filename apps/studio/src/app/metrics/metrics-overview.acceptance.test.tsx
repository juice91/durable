// SL.7 acceptance (tester): Metrics with the Overview header. Complements overview.test.tsx and
// overview.acceptance.test.tsx: parity per preset, filter and refresh consistency across all five
// sections, stale responses per section, error isolation, link round trips with filters, the `/`
// redirect table, phone-width guards and axe in both themes.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import MetricsPage from "./page";
import RootPage from "../page";
import { ALL_KPIS } from "@/components/metrics";
import { breakdownFixture, summaryFixture, timeseriesFixture } from "@/components/metrics/metricsFixtures";
import { computeDelta, parsePageState, pageStateToFilters } from "@/lib/metrics";
import { parseTraceFilters } from "@/lib/traces";
import { legacyTarget, routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

const push = vi.fn();
const replace = vi.fn();
let search = "";
const redirect = vi.hoisted(() =>
  vi.fn((url: string) => {
    throw new Error(`NEXT_REDIRECT:${url}`);
  }),
);
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
  useSearchParams: () => new URLSearchParams(search),
  redirect,
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  const list = vi.fn().mockResolvedValue([{ id: "a-1", name: "Support bot" }, { id: "a-2", name: "Intake" }]);
  return { ...original, api: { ...original.api, agents: { ...original.api.agents, list } } };
});

type Reply = { status: number; body: unknown };
type Responder = (url: URL) => Reply | Promise<Reply>;
const SUMMARY = summaryFixture(
  { runs: 12, completed: 6, failed: 3, needs_attention: 2, in_progress: 1, error_rate: 3 / 11, latency_p95_ms: 4200, requests: 9, input_tokens: 15000, output_tokens: 2500, cost_usd: "0.0420000000" },
  { runs: 6, completed: 4, failed: 2, needs_attention: 0, error_rate: 2 / 6, latency_p95_ms: 3000, requests: 4, input_tokens: 4000, output_tokens: 1000, cost_usd: "0.0100000000" },
);
const RUN = {
  run_id: "run-f1", kind: "agent", agent_id: "a-1", version: 1, status: "failed", display_status: "failed", input: "hi", output: null,
  error: "boom", heartbeat_at: null, created_at: "2026-11-12T09:00:00Z", updated_at: "2026-11-12T09:00:03Z", trace_id: null, conversation_id: null, turn: null,
};
const okResponder: Responder = (url) => {
  const p = url.pathname;
  if (p === "/api/v1/models") return { status: 200, body: { items: [{ id: "gpt-4o", provider: "openai" }] } };
  if (p === "/api/v1/workflows") return { status: 200, body: [] };
  if (p === "/api/v1/runs") return { status: 200, body: { items: [RUN], next_before: null } };
  if (p.endsWith("/summary")) return { status: 200, body: SUMMARY };
  if (p.endsWith("/timeseries")) return { status: 200, body: timeseriesFixture([{ runs: 5, failed: 1 }, { runs: 3 }, { runs: 4, failed: 2 }]) };
  if (url.searchParams.get("by") === "agent") return { status: 200, body: breakdownFixture("agent", [{ key: "a-1", label: "Support bot", runs: 12, failed: 3 }]) };
  return { status: 200, body: breakdownFixture("model", [{ key: "gpt-4o", runs: 9 }]) };
};
let calls: URL[] = [];
function mockFetch(responder: Responder) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = new URL(input);
    calls.push(url);
    const { status, body } = await responder(url);
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }));
}
/** The five data sections the page loads. */
const SECTIONS = ["summary", "timeseries", "agents", "models", "failures"] as const;
type Section = (typeof SECTIONS)[number];
function sectionOf(u: URL): Section | null {
  if (u.pathname.endsWith("/summary")) return "summary";
  if (u.pathname.endsWith("/timeseries")) return "timeseries";
  if (u.pathname === "/api/v1/runs") return "failures";
  if (u.pathname.endsWith("/breakdown")) return u.searchParams.get("by") === "agent" ? "agents" : "models";
  return null;
}
const forSection = (s: Section) => calls.filter((u) => sectionOf(u) === s);
const windowOf = (u: URL) => `${u.searchParams.get("from")}..${u.searchParams.get("to")}`;
const NOW = "2026-11-13T12:00:30Z";
const enc = (iso: string) => iso.replace(/:/g, "%3A");

function setup(timers: ("Date" | "setInterval" | "clearInterval")[] = ["Date"]) {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    push.mockReset();
    replace.mockReset();
    redirect.mockClear();
    search = "";
    document.documentElement.classList.remove("dark");
    vi.useFakeTimers({ toFake: timers });
    vi.setSystemTime(new Date(NOW));
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });
}

const CUSTOM = "range=custom&from=2026-11-01T00:00:00Z&to=2026-11-05T00:00:00Z";
const DASHBOARD_URLS = ["range=24h", "range=7d", "range=30d", "range=90d", CUSTOM];

describe("Redirected URL renders identically: every old dashboard URL equals the same Metrics URL", () => {
  setup();
  async function read(qs: string) {
    search = qs;
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12"));
    await waitFor(() => SECTIONS.forEach((s) => expect(forSection(s).length).toBeGreaterThan(0)));
    const kpis = Object.fromEntries(ALL_KPIS.map((id) => [id, [
      screen.getByTestId(`kpi-${id}-value`).textContent, screen.queryByTestId(`kpi-${id}-delta`)?.textContent ?? null,
    ]]));
    const queries = Object.fromEntries(SECTIONS.map((s) => [s, forSection(s).map((u) => u.search)]));
    const attention = screen.getByTestId("recent-failures-all").textContent;
    cleanup();
    return { kpis, queries, attention };
  }
  for (const qs of DASHBOARD_URLS) {
    it(`${qs}: identical queries, KPI numbers and deltas`, async () => {
      const target = legacyTarget("/", new URLSearchParams(qs))!;
      const url = new URL(target, "http://x");
      expect(url.pathname).toBe(routes.metrics());
      expect([...url.searchParams]).toEqual([...new URLSearchParams(qs)]);
      const viaLegacy = await read(url.search.slice(1));
      const direct = await read(qs);
      expect(viaLegacy).toEqual(direct);
      for (const s of SECTIONS) expect(direct.queries[s]).toHaveLength(1);
    });
  }

  it("an old dashboard URL with `interval` drops it (the dashboard ignored it)", () => {
    expect(legacyTarget("/", new URLSearchParams("range=7d&interval=hour"))).toBe(routes.metrics({ range: "7d" }));
    expect(legacyTarget("/", new URLSearchParams("interval=day"))).toBe(routes.agents());
  });

  for (const qs of ["", "range=30d", CUSTOM, "range=24h&agent_id=a-1&model=gpt-4o&provider=openai&kind=agent"]) {
    it(`restated P0.18 acceptance (${qs || "default"}): the summary and both breakdowns carry identical from, to and filter params`, async () => {
      search = qs;
      mockFetch(okResponder);
      render(<MetricsPage />);
      await waitFor(() => (["summary", "agents", "models"] as const).forEach((s) => expect(forSection(s)).toHaveLength(1)));
      const KEYS = ["from", "to", "agent_id", "model", "provider", "kind"];
      const shared = (u: URL) => KEYS.map((k) => [k, u.searchParams.get(k)]);
      const [summary, agents, models] = (["summary", "agents", "models"] as const).map((s) => shared(forSection(s)[0]));
      expect(summary.find(([k]) => k === "from")![1]).toBeTruthy();
      expect(agents).toEqual(summary);
      expect(models).toEqual(summary);
      if (qs.includes("agent_id")) expect(Object.fromEntries(summary)).toMatchObject({ agent_id: "a-1", model: "gpt-4o", provider: "openai", kind: "agent" });
    });
  }

  it("previous-period deltas are computed from the summary's previous values", async () => {
    const r = await read("range=7d");
    const runs = computeDelta(12, 6);
    expect(runs?.text).toBe("up 100% from 6");
    expect(r.kpis.runs[1]).toContain(runs!.text);
    expect(r.kpis.cost[1]).toMatch(/up 320% from/);
  });

  it("the Needs attention count is failed + needs_attention of the summary (3 + 2)", async () => {
    search = "range=30d";
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all 5 in Traces"));
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12");
  });
});

describe("Filters re-fetch every section with one consistent window", () => {
  setup();
  const CASES: [string, string, string, string][] = [
    ["Agent", "a-2", "agent_id", "a-2"],
    ["Model", "gpt-4o", "model", "gpt-4o"],
    ["Provider", "openai", "provider", "openai"],
    ["Kind", "workflow", "kind", "workflow"],
  ];
  for (const [label, value, param, expected] of CASES) {
    it(`choosing ${label} writes ${param} to the URL and every section re-fetches with it`, async () => {
      mockFetch(okResponder);
      const { rerender } = render(<MetricsPage />);
      await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12"));
      await waitFor(() => expect(screen.getByLabelText(label).querySelector(`option[value="${value}"]`)).not.toBeNull());
      const before = Object.fromEntries(SECTIONS.map((s) => [s, forSection(s).length]));
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
      expect(replace).toHaveBeenCalledTimes(1);
      const href = replace.mock.calls[0][0] as string;
      expect(new URL(href, "http://x").searchParams.get(param)).toBe(expected);
      search = new URL(href, "http://x").search.slice(1);
      rerender(<MetricsPage />);
      await waitFor(() => SECTIONS.forEach((s) => expect(forSection(s).length).toBeGreaterThan(before[s])));
      const fresh = SECTIONS.map((s) => forSection(s).slice(before[s]));
      for (const group of fresh) for (const u of group) expect(u.searchParams.get(param)).toBe(expected);
      const windows = new Set(fresh.flat().map(windowOf));
      expect(windows.size).toBe(1);
    });
  }
});

describe("Refresh and auto-refresh re-fetch every section together", () => {
  setup(["Date", "setInterval", "clearInterval"]);
  const counts = () => Object.fromEntries(SECTIONS.map((s) => [s, forSection(s).length]));
  const visibility = (state: "hidden" | "visible") => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
    document.dispatchEvent(new Event("visibilitychange"));
  };
  afterEach(() => visibility("visible"));

  async function loaded() {
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => SECTIONS.forEach((s) => expect(forSection(s)).toHaveLength(1)));
  }
  const allSectionsAt = (n: number) => waitFor(() => SECTIONS.forEach((s) => expect(forSection(s)).toHaveLength(n)));

  it("Refresh re-fetches all five sections once, in one new window", async () => {
    await loaded();
    await act(async () => { vi.advanceTimersByTime(60_000); });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await allSectionsAt(2);
    expect(new Set(SECTIONS.map((s) => windowOf(forSection(s)[1]))).size).toBe(1);
    expect(windowOf(forSection("summary")[1])).not.toBe(windowOf(forSection("summary")[0]));
  });

  it("the 5-minute auto-refresh re-fetches all five sections together", async () => {
    await loaded();
    await act(async () => { vi.advanceTimersByTime(5 * 60_000); });
    await allSectionsAt(2);
    expect(new Set(SECTIONS.map((s) => windowOf(forSection(s)[1]))).size).toBe(1);
  });

  it("is paused while the tab is hidden, and catches up once when visible again", async () => {
    await loaded();
    act(() => visibility("hidden"));
    await act(async () => { vi.advanceTimersByTime(20 * 60_000); });
    expect(counts()).toEqual({ summary: 1, timeseries: 1, agents: 1, models: 1, failures: 1 });
    act(() => visibility("visible"));
    await allSectionsAt(2);
    await act(async () => { vi.advanceTimersByTime(5 * 60_000); });
    await allSectionsAt(3);
  });

  it("a custom range does not auto-refresh but Refresh still re-fetches every section", async () => {
    search = CUSTOM;
    await loaded();
    await act(async () => { vi.advanceTimersByTime(15 * 60_000); });
    expect(counts()).toEqual({ summary: 1, timeseries: 1, agents: 1, models: 1, failures: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await allSectionsAt(2);
    expect(new Set(SECTIONS.map((s) => windowOf(forSection(s)[1])))).toEqual(new Set([windowOf(forSection("summary")[0])]));
  });
});

describe("A slow old-range response never overwrites the new range (each section)", () => {
  setup();
  const days = (u: URL) => Math.round((Date.parse(u.searchParams.get("to")!) - Date.parse(u.searchParams.get("from")!)) / 86_400_000);
  const oldTs = () => {
    const t = timeseriesFixture([{ runs: 1 }, { runs: 1 }, { runs: 1 }]);
    return { ...t, buckets: [...t.buckets, ...t.buckets.slice(0, 2).map((b, i) => ({ ...b, start: `2026-11-1${3 + i}T00:00:00+00:00` }))] };
  };
  const OLD: Record<Section, (u: URL) => Reply> = {
    summary: () => ({ status: 200, body: summaryFixture({ runs: 111, completed: 111 }, {}) }),
    timeseries: () => ({ status: 200, body: oldTs() }),
    agents: () => ({ status: 200, body: breakdownFixture("agent", [{ key: "old-agent", label: "Old agent", runs: 1 }]) }),
    models: () => ({ status: 200, body: breakdownFixture("model", [{ key: "old-model", runs: 1 }]) }),
    failures: () => ({ status: 200, body: { items: [{ ...RUN, run_id: "old-run", error: "old-error" }], next_before: null } }),
  };
  const bars = () => document.querySelectorAll('[data-testid="chart-Runs and failures"] a[data-bucket]').length;
  const showsOld: Record<Section, () => boolean> = {
    summary: () => screen.getByTestId("kpi-runs-value").textContent === "111",
    timeseries: () => bars() === 10,
    agents: () => screen.queryByTestId("breakdown-row-agent-old-agent") !== null,
    models: () => screen.queryByTestId("breakdown-row-model-old-model") !== null,
    failures: () => screen.queryByText("old-error") !== null,
  };
  for (const section of SECTIONS) {
    it(`${section}: the 7-day answer arriving after the 30-day one is dropped`, async () => {
      let release: () => void = () => undefined;
      const gate = new Promise<void>((r) => { release = r; });
      mockFetch(async (url) => {
        if (sectionOf(url) === section && days(url) === 7) { await gate; return OLD[section](url); }
        return okResponder(url);
      });
      const { rerender } = render(<MetricsPage />);
      await waitFor(() => expect(forSection(section)).toHaveLength(1));
      search = "range=30d";
      rerender(<MetricsPage />);
      await waitFor(() => SECTIONS.forEach((s) => expect(forSection(s).length).toBeGreaterThan(s === section ? 1 : 0)));
      await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12"));
      await waitFor(() => expect(bars()).toBe(6));
      await act(async () => { release(); await new Promise((r) => setTimeout(r, 30)); });
      expect(showsOld[section]()).toBe(false);
      expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12");
      expect(bars()).toBe(6);
    });
  }
});

describe("Section errors are isolated", () => {
  setup();
  for (const section of SECTIONS) {
    it(`${section} failing (500) leaves the other four sections rendered`, async () => {
      mockFetch((url) => (sectionOf(url) === section ? { status: 500, body: { detail: "kaput" } } : okResponder(url)));
      render(<MetricsPage />);
      await waitFor(() => SECTIONS.forEach((s) => expect(forSection(s).length).toBeGreaterThan(0)));
      const ok = {
        summary: () => screen.getByTestId("kpi-runs-value"),
        timeseries: () => screen.getByTestId("chart-Runs and failures"),
        agents: () => screen.getByTestId("breakdown-row-agent-a-1"),
        models: () => screen.getByTestId("breakdown-row-model-gpt-4o"),
        failures: () => screen.getByTestId("recent-failure"),
      };
      await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThanOrEqual(1));
      for (const s of SECTIONS) if (s !== section) await waitFor(() => expect(ok[s]()).toBeInTheDocument());
      expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
    });
  }
});

describe("Links carry the page filters and round-trip through the target's parser", () => {
  setup();
  const QS = "range=30d&model=gpt-4o&provider=openai&kind=agent";
  const FROM = "2026-10-14T12:01:00.000Z";
  const TO = "2026-11-13T12:01:00.000Z";
  const parse = (href: string) => new URL(href, "http://x").searchParams;
  const hrefOf = (id: string) => screen.getByTestId(id).getAttribute("href")!;

  it("KPI, Needs attention, Top agents and bar links keep model, provider, kind and the window", async () => {
    search = QS;
    mockFetch(okResponder);
    render(<MetricsPage />);
    await screen.findByTestId("recent-failure");
    await screen.findByTestId("breakdown-row-agent-a-1");
    const failedLink = screen.getByRole("link", { name: "Support bot: open 3 failed runs" }).getAttribute("href")!;
    const checks: [string, string, string[]][] = [
      ["kpi-runs", hrefOf("kpi-runs"), []],
      ["kpi-error_rate", hrefOf("kpi-error_rate"), ["failed"]],
      ["recent-failures-all", hrefOf("recent-failures-all"), ["failed", "needs_attention"]],
      ["agent failed count", failedLink, ["failed"]],
    ];
    for (const [name, href, status] of checks) {
      expect(new URL(href, "http://x").pathname, name).toBe(routes.traces());
      const f = parseTraceFilters(parse(href));
      expect([f.from, f.to, f.model, f.provider, f.kind, f.status], name).toEqual([FROM, TO, "gpt-4o", "openai", "agent", status]);
    }
    expect(parseTraceFilters(parse(failedLink)).agent_id).toBe("a-1");
    expect(hrefOf("recent-failures-all")).toBe(`${routes.traces()}?status=failed%2Cneeds_attention&from=${enc(FROM)}&to=${enc(TO)}&model=gpt-4o&provider=openai&kind=agent`);
    expect(screen.getByTestId("recent-failure")).toHaveAttribute("href", routes.trace("run-f1"));
  });

  it("the agent name link narrows Metrics, keeping every other filter, and parses back", async () => {
    search = QS;
    mockFetch(okResponder);
    render(<MetricsPage />);
    await screen.findByTestId("breakdown-row-agent-a-1");
    const href = screen.getByRole("link", { name: "Support bot: show this agent's metrics" }).getAttribute("href")!;
    expect(new URL(href, "http://x").pathname).toBe(routes.metrics());
    const state = parsePageState(parse(href));
    expect([state.range, state.agent_id, state.model, state.provider, state.kind]).toEqual(["30d", "a-1", "gpt-4o", "openai", "agent"]);
    const f = pageStateToFilters(state, new Date(NOW));
    expect([f.from, f.to]).toEqual([FROM, TO]);
  });

  it("an agent filter already set is kept on every Traces link", async () => {
    search = "agent_id=a-2";
    mockFetch(okResponder);
    render(<MetricsPage />);
    await screen.findByTestId("recent-failure");
    for (const id of ["kpi-runs", "kpi-error_rate", "recent-failures-all"]) {
      expect(parseTraceFilters(parse(hrefOf(id))).agent_id, id).toBe("a-2");
    }
  });
});

describe("The `/` redirect table at the page level", () => {
  type Search = Record<string, string | string[] | undefined>;
  async function to(searchParams: Search): Promise<string> {
    redirect.mockClear();
    await expect(RootPage({ searchParams: Promise.resolve(searchParams) })).rejects.toThrow("NEXT_REDIRECT:");
    return redirect.mock.calls[0][0];
  }
  const M = (qs: string) => `${routes.metrics()}?${qs}`;
  const rows: [string, Search, string][] = [
    ["bare", {}, routes.agents()],
    ["undefined values", { range: undefined }, routes.agents()],
    ["empty value is carried", { range: "" }, M("range=")],
    ["unknown only", { agent_id: "a-1", foo: "x" }, routes.agents()],
    ["unknown dropped, known kept", { foo: "x", range: "24h", agent_id: "a-1" }, M("range=24h")],
    ["array: every value, in order", { range: ["24h", "7d"] }, M("range=24h&range=7d")],
    ["bad range is passed through for Metrics to handle", { range: "bogus" }, M("range=bogus")],
    ["order of the params is the input order", { to: "2026-11-05T00:00:00Z", range: "custom", from: "2026-11-01T00:00:00Z" }, M(`to=${enc("2026-11-05T00:00:00Z")}&range=custom&from=${enc("2026-11-01T00:00:00Z")}`)],
    ["interval is dropped (the dashboard ignored it)", { interval: "hour", range: "7d" }, M("range=7d")],
    ["custom range, encoded", { range: "custom", from: "2026-11-01T00:00:00Z", to: "2026-11-05T00:00:00Z" }, M(`range=custom&from=${enc("2026-11-01T00:00:00Z")}&to=${enc("2026-11-05T00:00:00Z")}`)],
    ["a tricky value stays one encoded parameter", { range: "a&b=c#d" }, M("range=a%26b%3Dc%23d")],
  ];
  for (const [name, input, expected] of rows) {
    it(name, async () => {
      expect(await to(input)).toBe(expected);
      expect(redirect).toHaveBeenCalledTimes(1);
    });
  }

  it("the target of a dashboard link stays on this app (a relative path)", async () => {
    for (const input of [{ range: "https://evil.example" }, { from: "//evil.example", to: "x" }]) {
      const target = await to(input);
      expect(target.startsWith("/")).toBe(true);
      expect(target.startsWith("//")).toBe(false);
      expect(new URL(target, "http://studio.test").origin).toBe("http://studio.test");
    }
  });
});

describe("Phone width (400 px): class scan of the metrics components", () => {
  const SRC = join(__dirname, "..", "..");
  const files = [
    ...["components/metrics", "components/metrics/overview"].flatMap((d) =>
      readdirSync(join(SRC, d)).filter((f) => /\.(tsx|css)$/.test(f) && !/\.test\.|Fixtures/.test(f)).map((f) => join(SRC, d, f)),
    ),
    join(SRC, "app/metrics/page.tsx"),
  ];
  const px = (n: number, unit: string) => (unit === "rem" ? n * 16 : n);
  const WIDTH_LIMIT = 400;

  it("scans the Overview, KPI, chart, breakdown and page files", () => {
    const names = files.map((f) => f.split("/").pop());
    for (const n of ["MetricsOverview.tsx", "RecentFailures.tsx", "MetricsKpis.tsx", "BreakdownTable.tsx", "TimeSeriesChart.tsx", "page.tsx"]) expect(names).toContain(n);
  });

  it("no fixed or minimum width over 400 px (w-*, min-w-*, size-*, inline widths)", () => {
    const offenders: string[] = [];
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      // Fixed and minimum widths only: max-w-* caps never force overflow.
      const re = /(?<![\w-])(?:min-)?w-\[(\d+(?:\.\d+)?)(px|rem)\]|(?<![\w-])(?:min-)?w-(\d+)(?![\w./\[])|width:\s*(\d+(?:\.\d+)?)(px|rem)?|minWidth:\s*(\d+)/g;
      for (const m of text.matchAll(re)) {
        const value = m[1] ? px(Number(m[1]), m[2]) : m[3] ? Number(m[3]) * 4 : m[4] ? px(Number(m[4]), m[5] ?? "px") : Number(m[6]);
        if (value > WIDTH_LIMIT) offenders.push(`${file}: ${m[0]} = ${value}px`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("the KPI grid auto-fits with a minimum column that fits a phone, and no fixed column count", () => {
    const text = readFileSync(join(SRC, "components/metrics/MetricsKpis.tsx"), "utf8");
    expect(text).toContain("grid-cols-[repeat(auto-fit,minmax(min(100%,11rem),1fr))]");
    expect(text).not.toMatch(/(?<![\w-])(?:sm:|md:|lg:)?grid-cols-[1-9]\b/);
  });

  it("two-column layouts start at lg, and the page content and tables can shrink or scroll inside themselves", () => {
    const grid = readFileSync(join(SRC, "components/metrics/overview/MetricsOverview.tsx"), "utf8");
    expect(grid).toMatch(/grid gap-4 lg:grid-cols-2/);
    expect(grid).not.toMatch(/(?<![\w:-])grid-cols-2/);
    expect(readFileSync(join(SRC, "components/metrics/BreakdownTable.tsx"), "utf8")).toContain("overflow-x-auto");
    const page = readFileSync(join(SRC, "app/metrics/page.tsx"), "utf8");
    expect(page).toContain("mx-auto min-w-0 max-w-7xl");
  });
});

describe("Accessibility of the loaded Metrics page", () => {
  setup();
  async function loaded() {
    mockFetch(okResponder);
    render(<MetricsPage />);
    await screen.findByTestId("recent-failure");
    await screen.findByTestId("breakdown-row-agent-a-1");
    await screen.findByTestId("breakdown-row-model-gpt-4o");
    for (const t of ["Runs and failures", "Latency", "Tokens", "Cost (USD)"]) await screen.findByTestId(`chart-${t}`);
  }

  it.each(["light", "dark"])("has no axe violations in the %s theme", async (theme) => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    await loaded();
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("the Overview is a named region and the page has exactly one h1 (the page header)", async () => {
    await loaded();
    expect(screen.getByRole("region", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Needs attention" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Trends" })).toBeInTheDocument();
  });

  it("an empty tenant keeps the h1 count and passes axe", async () => {
    mockFetch((url) => (sectionOf(url) === "summary" ? { status: 200, body: summaryFixture({}, {}) } : okResponder(url)));
    render(<MetricsPage />);
    await screen.findByTestId("metrics-empty");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
