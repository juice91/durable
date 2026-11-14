/**
 * The blur-then-click race on the two filter pages that host the trace Sheet (SL.15 review I1, I2).
 * Typing in a text filter and then clicking a row's Trace link commits the filter on blur
 * (router.replace) and opens the Sheet in the same tick. Next 15.5 drops a replace still pending
 * when a new navigation arrives; `nextRouterMock` does the same, so these tests see the real
 * failure: without the fix the filter is lost (or comes back missing on close / Back).
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

import { nextRouter } from "./nextRouterMock";
import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";
import LogsPage from "@/app/logs/page";
import TracesPage from "@/app/traces/page";

const T0 = "2026-11-14T10:00:00Z";
/** A run id the Logs run filter accepts (a UUID). */
const R1 = "11111111-2222-3333-4444-555555555555";

function run(id: string) {
  return {
    run_id: id, kind: "agent", agent_id: "a1", version: 1, status: "completed", display_status: "completed",
    input: "hi", output: "ok", error: null, heartbeat_at: null, created_at: T0, updated_at: T0, trace_id: "t1",
    conversation_id: null, turn: null, started_at: T0, finished_at: T0,
  };
}

function respond(url: URL): unknown {
  const p = url.pathname.replace(/^\/api\/v1/, "");
  if (p === "/agents") return [{ id: "a1", name: "Support bot", latest_version: 1, draft_spec: { spec_version: 1, name: "Support bot", instructions: "x", model: { provider: "test", name: "echo" }, tools: [] } }];
  if (p === "/workflows") return [];
  if (p === "/logs") return { items: [{ ts: T0, level: "info", message: "run started", service: "worker", trace_id: "t1", span_id: "s1", run_id: R1 }], from: T0, to: T0, limit: 100 };
  if (p === "/runs") return { items: [run("r1"), run("r2")], next_before: null };
  if (/^\/runs\/[^/]+\/events$/.test(p)) return [];
  if (/^\/runs\/[^/]+$/.test(p)) return run(p.split("/").pop()!);
  return { items: [], next_before: null };
}

let media: MatchMediaControl;
beforeEach(() => {
  media = mockMatchMedia(true);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(typeof input === "string" ? input : (input as Request).url), "http://localhost");
    return new Response(JSON.stringify(respond(url)), { status: 200, headers: { "content-type": "application/json" } });
  }));
});
afterEach(() => {
  cleanup();
  media.restore();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

interface PageCase {
  name: string;
  path: string;
  Page: React.ComponentType;
  /** The text filter to type into, and what it writes to the URL. */
  input: () => HTMLElement;
  typed: string;
  param: string;
  /** The Sheet's parameter for the row's run. */
  trace: string;
  /** A row's Trace link. */
  link: () => Promise<HTMLElement>;
}

const PAGES: PageCase[] = [
  {
    name: "Logs (Run filter)",
    path: "/logs",
    Page: LogsPage,
    input: () => screen.getByLabelText("Run"),
    // A UUID commits on change (as the blur would for any other text): still pending at the click.
    typed: R1,
    param: `run_id=${R1}`,
    trace: `trace=${R1}`,
    link: async () => (await screen.findAllByRole("link", { name: `Open trace of run ${R1}` }))[0],
  },
  {
    name: "Traces (Model filter)",
    path: "/traces",
    Page: TracesPage,
    input: () => screen.getByLabelText("Model"),
    typed: "test:echo",
    param: "model=test%3Aecho",
    trace: "trace=r1",
    link: async () => (await screen.findAllByTestId("trace-row-open"))[0],
  },
];

async function settle() {
  await act(async () => {
    await nextRouter.settle();
    await nextRouter.settle();
  });
}

/** Type into the filter, then (same tick, as a real click does) blur it and click the row's Trace link. */
async function typeThenClickTrace(page: PageCase) {
  const link = await page.link();
  const input = page.input();
  fireEvent.change(input, { target: { value: page.typed } });
  fireEvent.blur(input);
  fireEvent.click(link);
  await settle();
}

describe.each(PAGES)("$name: a filter committed on blur by the click that opens the trace Sheet", (page) => {
  beforeEach(() => nextRouter.reset(["/agents", page.path]));

  test("is kept: the Sheet opens over the filtered list, and the box keeps the text", async () => {
    render(<FrameHarness><page.Page /></FrameHarness>);
    await typeThenClickTrace(page);
    // The model hit the race: the blur's replace was superseded by the Sheet's navigation.
    expect(nextRouter.dropped.some((href) => href.includes(page.param))).toBe(true);
    expect(nextRouter.url).toContain(page.param);
    expect(nextRouter.url).toContain(page.trace);
    expect(await screen.findByTestId("trace-sheet")).toBeInTheDocument();
    expect(page.input()).toHaveValue(page.typed);
    // One entry for the filtered list with the Sheet: no unfiltered page left behind it.
    expect(nextRouter.entries).toEqual(["/agents", nextRouter.url]);
  });

  test("Close lands on the filtered list", async () => {
    render(<FrameHarness><page.Page /></FrameHarness>);
    await typeThenClickTrace(page);
    const sheet = await screen.findByTestId("trace-sheet");
    fireEvent.click(within(sheet).getAllByRole("button", { name: "Close" })[0]);
    await settle();
    expect(nextRouter.url).toContain(page.param);
    expect(nextRouter.url).not.toContain("trace=");
    expect(nextRouter.entries).toEqual(["/agents", nextRouter.url]);
    expect(screen.queryByTestId("trace-sheet")).toBeNull();
    expect(page.input()).toHaveValue(page.typed);
  });

  test("Escape lands on the filtered list", async () => {
    render(<FrameHarness><page.Page /></FrameHarness>);
    await typeThenClickTrace(page);
    const sheet = await screen.findByTestId("trace-sheet");
    fireEvent.keyDown(sheet, { key: "Escape" });
    await settle();
    expect(nextRouter.url).toContain(page.param);
    expect(nextRouter.url).not.toContain("trace=");
    expect(screen.queryByTestId("trace-sheet")).toBeNull();
  });

  test("Back never lands on the unfiltered list (the Sheet's entry replaced it)", async () => {
    render(<FrameHarness><page.Page /></FrameHarness>);
    await typeThenClickTrace(page);
    act(() => nextRouter.back());
    await settle();
    expect(nextRouter.url).toBe("/agents");
    expect(nextRouter.entries).toEqual(["/agents"]);
  });

  test("without a write in flight, opening pushes: Back and Close both return to the (already filtered) list", async () => {
    render(<FrameHarness><page.Page /></FrameHarness>);
    const link = await page.link();
    const input = page.input();
    fireEvent.change(input, { target: { value: page.typed } });
    fireEvent.blur(input);
    await settle();
    const filtered = nextRouter.url;
    expect(filtered).toContain(page.param);
    fireEvent.click(link.isConnected ? link : await page.link());
    await settle();
    expect(nextRouter.entries).toEqual(["/agents", filtered, `${filtered}&${page.trace}`]);
    act(() => nextRouter.back());
    await settle();
    expect(nextRouter.url).toBe(filtered);
    expect(screen.queryByTestId("trace-sheet")).toBeNull();

    fireEvent.click(await page.link());
    await settle();
    const sheet = await screen.findByTestId("trace-sheet");
    fireEvent.click(within(sheet).getAllByRole("button", { name: "Close" })[0]);
    await settle();
    expect(nextRouter.url).toBe(filtered);
    expect(nextRouter.entries).toEqual(["/agents", filtered]);
  });
});
