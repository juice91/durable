import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Run } from "@/lib/runs";

const wfMock = vi.hoisted(() => ({ listWorkflowRuns: vi.fn() }));
const schedMock = vi.hoisted(() => ({ listSchedules: vi.fn() }));
vi.mock("@/lib/schedules", async (importOriginal) => ({ ...(await importOriginal<object>()), ...schedMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));

import { WorkflowRunHistory } from "./WorkflowRunHistory";

const run = (run_id: string, workflow_id: string): Run => ({
  run_id, kind: "workflow", workflow_id, workflow_version: 1, agent_id: null, version: null,
  status: "completed", display_status: "completed", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-13T10:00:00Z", updated_at: "2026-11-13T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
});

type Page = { items: Run[]; next_before: string | null };
function deferred() {
  let resolve!: (page: Page) => void;
  const promise = new Promise<Page>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeEach(() => wfMock.listWorkflowRuns.mockReset());
afterEach(() => cleanup());

describe("WorkflowRunHistory", () => {
  it("tags a scheduled run with the schedule's name, or just Scheduled before names load", async () => {
    schedMock.listSchedules.mockResolvedValue({ items: [{ id: "s1", name: "Hourly" }], next_before: null });
    wfMock.listWorkflowRuns.mockResolvedValue({ items: [{ ...run("cccccccc-1", "wf-c"), schedule_id: "s1" }], next_before: null });
    render(<WorkflowRunHistory workflowId="wf-c" />);
    expect(await screen.findByText(/^Scheduled/)).toHaveTextContent(/^Scheduled/);
    await act(async () => {});
    expect(screen.getByText(/^Scheduled/)).toHaveTextContent("Scheduled: Hourly");
  });

  it("ignores a stale load for the previous workflow after switching", async () => {
    const loadA = deferred();
    const loadB = deferred();
    wfMock.listWorkflowRuns.mockImplementation((id: string) => (id === "wf-a" ? loadA.promise : loadB.promise));
    const { rerender } = render(<WorkflowRunHistory workflowId="wf-a" />);
    rerender(<WorkflowRunHistory workflowId="wf-b" />);

    await act(async () => {
      loadB.resolve({ items: [run("bbbbbbbb-1", "wf-b")], next_before: null });
      await loadB.promise;
    });
    await act(async () => {
      loadA.resolve({ items: [run("aaaaaaaa-1", "wf-a")], next_before: null });
      await loadA.promise;
    });

    expect(screen.getByTestId("workflow-run-row-bbbbbbbb-1")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-row-aaaaaaaa-1")).toBeNull();
  });

  it("ignores a manual refresh of the previous workflow that resolves after switching", async () => {
    wfMock.listWorkflowRuns.mockResolvedValueOnce({ items: [run("aaaaaaaa-0", "wf-a")], next_before: null });
    const { rerender } = render(<WorkflowRunHistory workflowId="wf-a" />);
    await screen.findByTestId("workflow-run-row-aaaaaaaa-0");

    const refreshA = deferred();
    const loadB = deferred();
    wfMock.listWorkflowRuns.mockImplementation((id: string) => (id === "wf-a" ? refreshA.promise : loadB.promise));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    rerender(<WorkflowRunHistory workflowId="wf-b" />);
    // A's runs are not shown under B while B loads.
    expect(screen.queryByTestId("workflow-run-row-aaaaaaaa-0")).toBeNull();

    await act(async () => {
      loadB.resolve({ items: [run("bbbbbbbb-1", "wf-b")], next_before: null });
      await loadB.promise;
    });
    await act(async () => {
      refreshA.resolve({ items: [run("aaaaaaaa-2", "wf-a")], next_before: null });
      await refreshA.promise;
    });
    expect(screen.getByTestId("workflow-run-row-bbbbbbbb-1")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-row-aaaaaaaa-2")).toBeNull();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });
});
