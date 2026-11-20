import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { demoSpec } from "@/test/workflowFixtures";
import { routes } from "@/lib/routes";

const runsMock = vi.hoisted(() => ({ getRun: vi.fn(), listEvents: vi.fn(), subscribeRun: vi.fn() }));
const wfMock = vi.hoisted(() => ({ getWorkflowVersion: vi.fn(), listWorkflowRuns: vi.fn() }));

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { WorkflowRunView } from "./WorkflowRunView";

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  class DM { m22 = 1; constructor() {} }
  vi.stubGlobal("DOMMatrixReadOnly", DM);
});

const ORIGINAL = "aaaaaaaa-1111-2222-3333-444444444444";
const NEWER = "bbbbbbbb-1111-2222-3333-444444444444";
const OLDER = "cccccccc-1111-2222-3333-444444444444";

const run = (over: Record<string, unknown> = {}) => ({
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 1, agent_id: null, version: null,
  status: "completed", display_status: "completed", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-13T10:00:00Z", updated_at: "2026-11-13T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
  rerun_of: null, ...over,
});

beforeEach(() => {
  runsMock.getRun.mockResolvedValue(run());
  runsMock.listEvents.mockResolvedValue([]);
  runsMock.subscribeRun.mockReturnValue(vi.fn());
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: demoSpec, spec_hash: "h", created_at: "", created_by: "u" });
  wfMock.listWorkflowRuns.mockResolvedValue({ items: [], next_before: null });
});
afterEach(() => cleanup());

async function renderView(runId = "run-1") {
  render(<WorkflowRunView runId={runId} workflowId="wf-1" />);
  await screen.findByTestId("workflow-step-classify");
}

describe("WorkflowRunView re-run back-links", () => {
  it("links 'Re-run of' to the original run when rerun_of is set", async () => {
    runsMock.getRun.mockResolvedValue(run({ rerun_of: ORIGINAL }));
    await renderView();
    const note = await screen.findByTestId("workflow-run-rerun-of");
    expect(note).toHaveTextContent(`Re-run of ${ORIGINAL.slice(0, 8)}`);
    expect(note.querySelector("a")).toHaveAttribute("href", routes.workflowRun("wf-1", ORIGINAL));
  });

  it("shows no 'Re-run of' when rerun_of is null", async () => {
    await renderView();
    expect(screen.queryByTestId("workflow-run-rerun-of")).toBeNull();
  });

  it("links 'Re-run as' to the newest run that re-ran this one, from the run history", async () => {
    wfMock.listWorkflowRuns.mockResolvedValue({
      items: [
        run({ run_id: NEWER, rerun_of: "run-1", created_at: "2026-11-14T10:00:00Z" }),
        run({ run_id: OLDER, rerun_of: "run-1", created_at: "2026-11-13T12:00:00Z" }),
        run({ run_id: "dddddddd", rerun_of: "other", created_at: "2026-11-15T10:00:00Z" }),
      ],
      next_before: null,
    });
    await renderView();
    const note = await screen.findByTestId("workflow-run-rerun-as");
    expect(note).toHaveTextContent(`Re-run as ${NEWER.slice(0, 8)}`);
    expect(note.querySelector("a")).toHaveAttribute("href", routes.workflowRun("wf-1", NEWER));
  });

  it("shows no 'Re-run as' when nothing re-ran this run", async () => {
    await renderView();
    await waitFor(() => expect(wfMock.listWorkflowRuns).toHaveBeenCalled());
    expect(screen.queryByTestId("workflow-run-rerun-as")).toBeNull();
  });
});

describe("WorkflowRunView recovered badge", () => {
  it("shows Recovered ×2 in the header for a run DBOS resumed twice", async () => {
    runsMock.getRun.mockResolvedValue(run({ recovered: true, recovery_attempts: 3 }));  // attempts=3 means 2 restarts
    await renderView();
    expect(await screen.findByTestId("run-recovered-badge")).toHaveTextContent("Recovered ×2");
  });

  it("shows no badge for a run that was never resumed", async () => {
    runsMock.getRun.mockResolvedValue(run({ recovered: false, recovery_attempts: 1 }));  // attempts=1 means never recovered
    await renderView();
    expect(screen.queryByTestId("run-recovered-badge")).toBeNull();
  });
});
