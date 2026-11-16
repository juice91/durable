import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { AttachedScorers } from "@/components/agentPage/AttachedScorers";
import { TraceScores } from "@/components/traces/TraceScores";
import { evaluatorFromForm } from "@/lib/scorers";
import { ReusableScorers } from "./ReusableScorers";

vi.mock("next/navigation", () => ({
  usePathname: () => "/scorers",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const T = "2026-11-16T10:00:00Z";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const polite = { id: "s1", name: "polite", description: null, evaluator: { kind: "contains", value: "thanks" }, agent_ids: [], created_at: T, updated_at: T };

let calls: { method: string; path: string; body: unknown }[];
let store: { scorers: unknown[]; attached: unknown[]; events: unknown[] };

beforeEach(() => {
  calls = [];
  store = { scorers: [polite], attached: [], events: [] };
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const path = url.pathname.replace("/api/v1", "");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ method, path, body });
    if (path === "/scorers" && method === "GET") return json({ items: store.scorers });
    if (path === "/scorers" && method === "POST") {
      store.scorers = [...store.scorers, { ...polite, id: "s2", name: body.name, evaluator: body.evaluator }];
      return json(store.scorers[1], 201);
    }
    if (path === "/scorers/s1" && method === "DELETE") { store.scorers = []; return new Response(null, { status: 204 }); }
    if (path === "/agents/a1/scorers" && method === "GET") return json({ items: store.attached });
    if (path === "/agents/a1/scorers/s1" && method === "PUT") {
      store.attached = [{ id: "s1", name: "polite", evaluator: polite.evaluator, sampling_rate: body.sampling_rate }];
      return json({ items: store.attached });
    }
    if (path === "/agents/a1/scorers/s1" && method === "DELETE") { store.attached = []; return new Response(null, { status: 204 }); }
    if (path === "/runs/r1/events") return json(store.events);
    return json({ detail: "missing" }, 404);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe("evaluatorFromForm", () => {
  it("builds each kind and refuses bad input", () => {
    expect(evaluatorFromForm("contains", "hi", "")).toEqual({ kind: "contains", value: "hi", case_sensitive: false });
    expect(evaluatorFromForm("regex", " ", "")).toBe("Enter a pattern.");
    expect(evaluatorFromForm("max_cost", "0", "")).toMatch(/above zero/);
    expect(evaluatorFromForm("field_equals", "a.b", "3")).toEqual({ kind: "field_equals", path: "a.b", value: 3 });
    expect(evaluatorFromForm("field_equals", "a.b", "nope")).toMatch(/JSON/);
  });
});

describe("ReusableScorers", () => {
  it("lists scorers, creates one and deletes one", async () => {
    render(<ReusableScorers />);
    const row = await screen.findByTestId("reusable-scorer");
    expect(within(row).getByText("polite")).toBeInTheDocument();
    expect(row).toHaveTextContent("contains “thanks”");

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "digits" } });
    fireEvent.change(screen.getByLabelText("Check"), { target: { value: "regex" } });
    fireEvent.change(screen.getByLabelText("Pattern"), { target: { value: "\\d+" } });
    fireEvent.click(screen.getByRole("button", { name: "Add scorer" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ name: "digits", evaluator: { kind: "regex", pattern: "\\d+" } });

    fireEvent.click(await screen.findByRole("button", { name: "Delete scorer polite" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path === "/scorers/s1")).toBe(true));
  });

  it("shows a form error without calling the API", async () => {
    render(<ReusableScorers />);
    await screen.findByTestId("reusable-scorer");
    fireEvent.click(screen.getByRole("button", { name: "Add scorer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a name.");
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<ReusableScorers />);
    await screen.findByTestId("reusable-scorer");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("AttachedScorers", () => {
  it("attaches with a sampling rate and detaches", async () => {
    render(<AttachedScorers agentId="a1" />);
    expect(await screen.findByText("No scorers attached.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Scorer"), { target: { value: "s1" } });
    fireEvent.change(screen.getByLabelText("Sample %"), { target: { value: "25" } });
    fireEvent.click(screen.getByRole("button", { name: "Attach" }));
    const row = await screen.findByTestId("attached-scorer");
    expect(row).toHaveTextContent("25% of runs");
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ sampling_rate: 0.25 });
    fireEvent.click(screen.getByRole("button", { name: "Detach scorer polite" }));
    await waitFor(() => expect(screen.queryByTestId("attached-scorer")).toBeNull());
  });

  it("refuses a sampling rate over 100", async () => {
    render(<AttachedScorers agentId="a1" />);
    await screen.findByText("No scorers attached.");
    fireEvent.change(screen.getByLabelText("Scorer"), { target: { value: "s1" } });
    fireEvent.change(screen.getByLabelText("Sample %"), { target: { value: "150" } });
    fireEvent.click(screen.getByRole("button", { name: "Attach" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("sampling rate");
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });
});

describe("TraceScores", () => {
  it("lists only the score events of the run", async () => {
    store.events = [
      { seq: 0, type: "run_started", data: {}, ts: T },
      { seq: 5, type: "score", ts: T, data: { scorer: "polite", scorer_id: "s1", score: 1, passed: true, reason: null } },
      { seq: 6, type: "score", ts: T, data: { scorer: "short", scorer_id: "s2", score: 0, passed: false, reason: "the output does not match the pattern" } },
    ];
    render(<TraceScores runId="r1" />);
    const rows = await screen.findAllByTestId("trace-score");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("polite");
    expect(rows[0]).toHaveTextContent("Passed");
    expect(rows[1]).toHaveTextContent("Failed");
    expect(rows[1]).toHaveTextContent("does not match the pattern");
  });

  it("explains an empty list", async () => {
    render(<TraceScores runId="r1" />);
    expect(await screen.findByTestId("trace-scores-empty")).toHaveTextContent("Attach scorers");
  });
});
