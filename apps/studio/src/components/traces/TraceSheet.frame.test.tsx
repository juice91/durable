/**
 * SL.12: the run views inside the real frame. `/traces/:runId` and `/runs/:id` are redirects now, so
 * the frame check that covered those pages (frame.acceptance.test.tsx, light and dark) covers the
 * trace Sheet open over the Traces list instead: on its Spans tab (the old trace page) and its Events
 * tab (the old run page). One main, Traces active in the sidebar, the Sheet named, no axe violations.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import "@testing-library/jest-dom";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import React from "react";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { routes } from "@/lib/routes";
import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";

const nav = vi.hoisted(() => ({ path: "/traces", search: "" }));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));

const RUN = "11111111-2222-3333-4444-555555555555";
const AGENT = {
  id: "a1",
  name: "Support bot",
  latest_version: 1,
  draft_spec: { spec_version: 1, name: "Support bot", instructions: "Help.", model: { provider: "test", name: "echo" }, tools: [] },
};
const RUN_ROW = {
  run_id: RUN, kind: "agent", agent_id: "a1", version: 1, status: "completed", display_status: "completed", input: "hi", output: "ok",
  error: null, heartbeat_at: null, created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:01Z", trace_id: null, conversation_id: null, turn: null,
};

function respond(url: URL): unknown {
  const p = url.pathname;
  if (p === `/api/v1/runs/${RUN}/trace`) {
    return {
      run: RUN_ROW,
      spans: [
        { id: "root", parent_id: null, kind: "run", name: "agent run", start: "2026-11-14T10:00:00Z", end: "2026-11-14T10:00:01Z", status: "ok", attributes: { run_kind: "agent", run_id: RUN, agent_id: "a1" }, input: null, output: null, truncated: {} },
      ],
      totals: { runs: 1, model_requests: 0, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 1000, status: "completed" },
      truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
      limits: { max_spans: 2000, max_events_per_run: 10000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
    };
  }
  if (/^\/api\/v1\/runs\/[^/]+\/events$/.test(p)) return [];
  if (p === `/api/v1/runs/${RUN}`) return RUN_ROW;
  if (p === "/api/v1/runs") return { items: [RUN_ROW], next_before: null };
  if (p === "/api/v1/agents") return [AGENT];
  if (p === "/api/v1/workflows") return [];
  if (p === "/api/v1/logs") return { items: [], from: "2026-11-14T09:00:00Z", to: "2026-11-14T10:00:00Z", limit: 500 };
  return { items: [], next_before: null, total: 0 };
}

let media: MatchMediaControl | undefined;

beforeEach(() => {
  document.documentElement.lang = "en";
  window.localStorage.clear();
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => {
      const url = new URL(String(typeof input === "string" ? input : (input as Request).url), "http://localhost");
      return new Response(JSON.stringify(respond(url)), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
  // The run's live stream is not part of this check.
  vi.stubGlobal("EventSource", class { close() {} addEventListener() {} removeEventListener() {} });
});

afterEach(() => {
  cleanup();
  media?.restore();
  media = undefined;
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  document.documentElement.classList.remove("dark");
});

describe.each(["light", "dark"] as const)("the trace Sheet inside the frame (%s theme)", (theme) => {
  beforeEach(() => {
    window.localStorage.setItem("studio.theme", theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
  });

  test.each([
    ["spans", "trace-span-row"],
    ["events", "run-events-panel"],
  ] as const)("open on %s: one main, Traces active, the Sheet named, no axe violations", async (tab, contentId) => {
    nav.path = routes.traces();
    nav.search = new URL(tab === "events" ? routes.run(RUN) : routes.trace(RUN), "http://studio.test").search.slice(1);
    media = mockMatchMedia(true);
    const { default: TracesPage } = await import("@/app/traces/page");
    render(
      <FrameHarness>
        <TracesPage />
      </FrameHarness>,
    );
    const sheet = await screen.findByTestId("trace-sheet");
    expect((await within(sheet).findAllByTestId(contentId)).length).toBeGreaterThan(0);
    expect(within(sheet).getByRole("tab", { name: tab === "events" ? "Events" : "Spans" })).toHaveAttribute("aria-selected", "true");
    expect(sheet).toHaveAccessibleName();
    expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark");
    const active = document.querySelectorAll('nav [aria-current="page"][data-nav-id]');
    expect(active).toHaveLength(1);
    expect(active[0].getAttribute("data-nav-id")).toBe("traces");
    expect(document.querySelectorAll("main, [role='main']")).toHaveLength(1);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    await expectNoAxeViolations(document, { rules: { "landmark-one-main": { enabled: true }, region: { enabled: true } } });
  }, AXE_TIMEOUT_MS);
});
