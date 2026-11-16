import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Agent } from "@/lib/api";
import { RunDialog } from "./RunDialog";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const suite = { id: "s1", agent_id: null, workflow_id: "wf1", name: "Suite", description: null, evaluators: [], case_count: 2, archived: false, archived_at: null, created_at: null, updated_at: null };
const started = { run_id: "r1", status: "queued", display_status: "queued", agent_id: null, version: 2, draft: false, suite_id: "s1", suite_hash: "h", cases: 2, max_concurrency: 4, trace_id: null, warnings: [] };

let calls: Array<{ method: string; path: string; query: URLSearchParams; body: Record<string, unknown> | null }>;

beforeEach(() => {
  calls = [];
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const path = url.pathname.replace("/api/v1", "");
    calls.push({ method: init?.method ?? "GET", path, query: url.searchParams, body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
    if (path.endsWith("/evals/warnings")) return json({ version: 1, draft: false, warnings: [] });
    if (path.endsWith("/evals/runs")) return json(started, 201);
    return json({ detail: "missing" }, 404);
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("RunDialog", () => {
  it("runs a workflow suite against a published version through the workflow route, without a draft", async () => {
    const onStarted = vi.fn();
    render(<RunDialog target={{ kind: "workflow", id: "wf1", name: "Triage" }} versions={[{ version: 1 }, { version: 2 }]} suite={suite} hasMemory={false} onStarted={onStarted} onClose={vi.fn()} />);
    const select = screen.getByTestId("eval-run-target") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["2", "1"]);
    expect(select.value).toBe("2");
    expect(screen.getByTestId("eval-run-dialog")).toHaveTextContent("its own workflow run");
    await waitFor(() => expect(screen.getByTestId("eval-run-start")).toBeEnabled());
    fireEvent.click(screen.getByTestId("eval-run-start"));
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(started));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe("/workflows/wf1/evals/runs");
    expect(post.body).toMatchObject({ suite_id: "s1", version: 2, max_concurrency: 4 });
    expect(calls.some((c) => c.path.endsWith("/evals/warnings"))).toBe(false); // no tool warnings for a workflow
  });

  it("runs an agent suite against its route as before, with a draft option and a warnings check", async () => {
    const agent = { id: "a1", name: "Bot", latest_version: 1, draft_spec: null } as unknown as Agent;
    const agentSuite = { ...suite, agent_id: "a1", workflow_id: null };
    const onStarted = vi.fn();
    render(<RunDialog target={{ kind: "agent", id: "a1", name: "Bot" }} agent={agent} versions={[{ version: 1 }]} suite={agentSuite} hasMemory={false} onStarted={onStarted} onClose={vi.fn()} />);
    const select = screen.getByTestId("eval-run-target") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["1", "draft"]);
    await waitFor(() => expect(calls.some((c) => c.path === "/agents/a1/evals/warnings")).toBe(true));
    await waitFor(() => expect(screen.getByTestId("eval-run-start")).toBeEnabled());
    fireEvent.click(screen.getByTestId("eval-run-start"));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(calls.find((c) => c.method === "POST")!.path).toBe("/agents/a1/evals/runs");
  });

  it("says a workflow without a published version has none", () => {
    render(<RunDialog target={{ kind: "workflow", id: "wf1", name: "Triage" }} versions={[]} suite={suite} hasMemory={false} onStarted={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByText("The workflow has no published version.")).toBeInTheDocument();
    expect(screen.getByTestId("eval-run-start")).toBeDisabled();
  });
});
