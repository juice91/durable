import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProcessorsPage from "./page";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));

const CATALOG = {
  items: [
    {
      id: "pii_redact", title: "PII redaction", description: "Detects personal data.", phases: ["input", "output"], actions: ["redact", "block"], deterministic: true,
      config_schema: { type: "object", properties: { kinds: { type: "array", default: ["email"], items: { type: "string", enum: ["email", "phone"] } }, action: { type: "string", default: "redact", enum: ["redact", "block"] } } },
    },
    {
      id: "moderation", title: "Moderation", description: "Asks a model.", phases: ["input", "output"], actions: ["block"], deterministic: false,
      config_schema: { type: "object", required: ["model"], properties: { model: { type: "object", properties: { provider: { type: "string" }, name: { type: "string" } } } } },
    },
  ],
  limits: { max_processors_per_phase: 8, max_regex_patterns: 20, max_regex_pattern_length: 200, max_test_sample_bytes: 32768 },
  moderation_categories: ["hate"],
};

const RUN = "11111111-2222-3333-4444-555555555555";
const row = (over: Record<string, unknown> = {}) => ({
  run_id: RUN, seq: 1, ts: "2026-11-13T10:00:00Z", agent_id: "agent-1", agent_version: 1, parent_run_id: null,
  phase: "input", index: 0, processor_type: "pii_redact", action: "redact", match_counts: { email: 1 }, ...over,
});

type Handler = (url: URL, init?: RequestInit) => { status: number; body: unknown } | Promise<{ status: number; body: unknown }>;
let calls: { url: URL; init?: RequestInit }[] = [];
let agentsBody: unknown[] = [];
let handlers: Record<string, Handler> = {};

function installFetch() {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input);
    calls.push({ url, init });
    const path = url.pathname.replace("/api/v1", "");
    let res: { status: number; body: unknown } = { status: 404, body: { detail: "not found" } };
    if (handlers[path]) res = await handlers[path](url, init);
    else if (path === "/processors") res = { status: 200, body: CATALOG };
    else if (path === "/agents") res = { status: 200, body: agentsBody };
    else if (path === "/processor-results") res = { status: 200, body: { items: [], next_before: null } };
    else if (/^\/agents\/[^/]+\/versions\/\d+$/.test(path)) res = { status: 200, body: { spec: {} } };
    return new Response(JSON.stringify(res.body), { status: res.status, headers: { "Content-Type": "application/json" } });
  }));
}

const agent = (n: number) => ({
  id: `agent-${n}`, name: `Agent ${n}`, latest_version: 1,
  draft_spec: { spec_version: 1, name: `Agent ${n}`, instructions: "", model: { provider: "test", name: "m" }, tools: [] },
});

describe("Processors page acceptance", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    agentsBody = [agent(1)];
    handlers = {};
    installFetch();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("never shows matched text, even if the API sent some", async () => {
    handlers["/processor-results"] = () => ({
      status: 200,
      body: { items: [row({ matched_text: "alice@example.com", match: "alice@example.com", categories: ["hate"], processor_type: "moderation", match_counts: {} })], next_before: null },
    });
    render(<ProcessorsPage />);
    const r = await screen.findByTestId("result-row");
    expect(r).toHaveTextContent("Categories: hate");
    expect(document.body.textContent).not.toContain("alice@example.com");
    expect(screen.getByText(/Matched text is never stored/)).toBeInTheDocument();
  });

  it("the test console refuses moderation: the option is disabled and the API 422 is mapped", async () => {
    handlers["/processors/test"] = () => ({ status: 422, body: { detail: "moderation calls a model and cannot run in the test console; test it in the playground" } });
    render(<ProcessorsPage />);
    const option = await screen.findByRole("option", { name: "Moderation (test in the playground)" });
    expect(option).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(await screen.findByTestId("console-error")).toHaveTextContent("Test it in the playground");
  });

  it("outcome filters reach the API and Reset clears them", async () => {
    render(<ProcessorsPage />);
    await screen.findByRole("option", { name: "Agent 1" });
    const filters = within(screen.getByRole("form", { name: "Filter outcomes" }));
    fireEvent.change(filters.getByLabelText("Processor"), { target: { value: "pii_redact" } });
    fireEvent.change(filters.getByLabelText("Action"), { target: { value: "redact" } });
    fireEvent.click(filters.getByRole("button", { name: "Apply" }));
    await waitFor(() => {
      const last = calls.filter((c) => c.url.pathname.endsWith("/processor-results")).at(-1)!.url.searchParams;
      expect(Object.fromEntries(last)).toEqual({ type: "pii_redact", action: "redact", limit: "50" });
    });
    fireEvent.click(filters.getByRole("button", { name: "Reset" }));
    await waitFor(() => {
      const last = calls.filter((c) => c.url.pathname.endsWith("/processor-results")).at(-1)!.url.searchParams;
      expect(Object.fromEntries(last)).toEqual({ limit: "50" });
    });
  });

  it("refuses From after To without calling the API", async () => {
    render(<ProcessorsPage />);
    await screen.findByRole("option", { name: "Agent 1" });
    const before = calls.length;
    const filters = within(screen.getByRole("form", { name: "Filter outcomes" }));
    fireEvent.change(filters.getByLabelText("From"), { target: { value: "2026-11-13T10:00" } });
    fireEvent.change(filters.getByLabelText("To"), { target: { value: "2026-11-12T10:00" } });
    fireEvent.click(filters.getByRole("button", { name: "Apply" }));
    expect(await screen.findByTestId("results-filter-error")).toHaveTextContent("From must be before To");
    expect(calls.length).toBe(before);
  });

  it("pages older outcomes by cursor and hides the button on the last page", async () => {
    let n = 0;
    handlers["/processor-results"] = (url) => {
      n += 1;
      if (url.searchParams.get("before") === "c1") return { status: 200, body: { items: [row({ seq: 1 })], next_before: null } };
      return { status: 200, body: { items: [row({ seq: 2 })], next_before: "c1" } };
    };
    render(<ProcessorsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Load older outcomes" }));
    await waitFor(() => expect(screen.getAllByTestId("result-row")).toHaveLength(2));
    expect(n).toBe(2);
    expect(screen.queryByRole("button", { name: "Load older outcomes" })).toBeNull();
  });

  it("drops a slow answer for filters that were replaced", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => { release = r; });
    handlers["/processor-results"] = async (url) => {
      if (!url.searchParams.get("action")) {
        if (calls.filter((c) => c.url.pathname.endsWith("/processor-results")).length === 1) {
          await gate;
          return { status: 200, body: { items: [row({ run_id: "99999999-0000-0000-0000-000000000000", action: "redact" })], next_before: null } };
        }
      }
      return { status: 200, body: { items: [row({ action: "block" })], next_before: null } };
    };
    render(<ProcessorsPage />);
    await screen.findByRole("option", { name: "Agent 1" });
    const filters = within(screen.getByRole("form", { name: "Filter outcomes" }));
    fireEvent.change(filters.getByLabelText("Action"), { target: { value: "block" } });
    fireEvent.click(filters.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(screen.getByTestId("result-row")).toHaveAttribute("data-run-id", RUN));
    await act(async () => { release(); });
    expect(screen.getAllByTestId("result-row")).toHaveLength(1);
    expect(screen.getByTestId("result-row")).toHaveAttribute("data-run-id", RUN);
  });

  it("Used by reads at most 4 agent versions at a time", async () => {
    agentsBody = Array.from({ length: 10 }, (_, i) => agent(i + 1));
    let inFlight = 0;
    let peak = 0;
    handlers = {};
    for (let i = 1; i <= 10; i++) {
      handlers[`/agents/agent-${i}/versions/1`] = async () => {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        await new Promise((r) => setTimeout(r, 5));
        inFlight -= 1;
        return { status: 200, body: { spec: { input_processors: [{ type: "pii_redact", config: {} }] } } };
      };
    }
    render(<ProcessorsPage />);
    await waitFor(() => expect(calls.filter((c) => /\/versions\/1$/.test(c.url.pathname))).toHaveLength(10));
    await screen.findByTestId("processor-usage");
    expect(peak).toBeGreaterThan(1);
    expect(peak).toBeLessThanOrEqual(4);
  });

  it("an unreadable version is 'unknown', not 'unused'", async () => {
    handlers["/agents/agent-1/versions/1"] = () => ({ status: 500, body: { detail: "boom" } });
    render(<ProcessorsPage />);
    const cards = await screen.findAllByTestId("processor-card");
    const pii = cards.find((c) => c.dataset.processorType === "pii_redact")!;
    expect(await within(pii).findByTestId("processor-usage-unknown")).toBeInTheDocument();
    expect(within(pii).queryByTestId("processor-usage-empty")).toBeNull();
  });

  it("the test console keeps the loc of a 422 detail list", async () => {
    handlers["/processors/test"] = () => ({
      status: 422,
      body: { detail: [{ loc: ["body", "processors", 0, "config", "max_chars"], msg: "Input should be greater than or equal to 1", type: "greater_than_equal" }] },
    });
    render(<ProcessorsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Run test" }));
    expect(await screen.findByTestId("console-error")).toHaveTextContent("processors.0.config.max_chars: Input should be greater than or equal to 1");
  });
});
