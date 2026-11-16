// LP.12 review: the run page reads and writes the pagers' URL state (?iter=, ?failedOnly=).
import React from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunEvent, SubscribeHandlers } from "@/lib/runs";
import { foreachSpec } from "@/test/workflowLoopFixtures";

const runsMock = vi.hoisted(() => ({ getRun: vi.fn(), listEvents: vi.fn(), subscribeRun: vi.fn() }));
const wfMock = vi.hoisted(() => ({ getWorkflowVersion: vi.fn() }));
const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), search: new URLSearchParams("iter=each.0") }));

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "wf-1", runId: "run-1" }),
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
  useSearchParams: () => nav.search,
  usePathname: () => "/workflows/wf-1/runs/run-1",
}));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import WorkflowRunPage from "@/app/workflows/[id]/runs/[runId]/page";

let handlers: SubscribeHandlers;
let seq = 0;
const ev = (type: RunEvent["type"], data: Record<string, unknown>): RunEvent => ({ seq: seq++, type, data, ts: "2026-11-15T10:00:00Z" });
const RUN = {
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 1, agent_id: null, version: null,
  status: "running", display_status: "running", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-15T10:00:00Z", updated_at: "2026-11-15T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
};

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});

beforeEach(() => {
  seq = 0;
  localStorage.clear();
  nav.replace.mockReset();
  runsMock.getRun.mockResolvedValue(RUN);
  runsMock.listEvents.mockResolvedValue([]);
  runsMock.subscribeRun.mockImplementation((_id: string, h: SubscribeHandlers) => {
    handlers = h;
    return () => undefined;
  });
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: foreachSpec, spec_hash: "h", created_at: "", created_by: "u" });
});

afterEach(() => cleanup());

describe("workflow run page iteration URL state", () => {
  it("opens on the iteration ?iter= names and writes the pager's moves to the URL", async () => {
    render(<WorkflowRunPage />);
    await screen.findByTestId("workflow-step-triage");
    act(() => {
      for (const index of [0, 1, 2]) {
        handlers.onEvent?.(ev("loop_iteration_started", { step_id: "each", scope: [], index }));
        handlers.onEvent?.(ev("step_started", { step_id: "triage", step_type: "agent", scope: [{ loop: "each", index }] }));
      }
    });
    const pager = screen.getByTestId("workflow-iteration-pager-each");
    expect(within(pager).getByTestId("workflow-iteration-pager-label")).toHaveTextContent("Iteration 1 of 3");
    expect(pager).toHaveAttribute("data-following", "false");
    fireEvent.click(within(pager).getByRole("button", { name: "Next iteration" }));
    const target = new URL(String(nav.replace.mock.calls.at(-1)![0]), "http://studio.test");
    expect(target.searchParams.get("iter")).toBe("each.1");
    fireEvent.click(within(pager).getByTestId("workflow-iteration-pager-latest"));
    const back = new URL(String(nav.replace.mock.calls.at(-1)![0]), "http://studio.test");
    expect(back.searchParams.has("iter")).toBe(false);
  });
});
