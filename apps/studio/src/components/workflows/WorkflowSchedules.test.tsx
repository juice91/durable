import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const sched = vi.hoisted(() => ({ listSchedules: vi.fn(), createSchedule: vi.fn(), updateSchedule: vi.fn(), deleteSchedule: vi.fn() }));
vi.mock("@/lib/schedules", async (importOriginal) => ({ ...(await importOriginal<object>()), ...sched }));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("next/navigation", () => ({ usePathname: () => "/workflows/w1/schedules", useParams: () => ({}), useRouter: () => ({ push: vi.fn() }), useSearchParams: () => new URLSearchParams() }));

import { WorkflowSchedules } from "./WorkflowSchedules";
import { workflowTabs } from "./WorkflowHeader";
import type { WorkflowSchedule } from "@/lib/schedules";

const base: WorkflowSchedule = {
  id: "s1", workflow_id: "w1", workflow_name: "Triage", name: "Hourly", cron: "5 * * * *", timezone: "UTC", input: {}, active: true,
  last_error: null, last_fired_at: null, next_fire_at: "2026-11-17T10:05:00Z", created_at: "2026-11-17T09:00:00Z", updated_at: "2026-11-17T09:00:00Z",
};
const stopped: WorkflowSchedule = { ...base, id: "s2", name: "Nightly", active: false, last_error: "input_schema_errors: topic is required" };
const paused: WorkflowSchedule = { ...base, id: "s3", name: "Weekly", active: false };

beforeEach(() => {
  for (const fn of Object.values(sched)) fn.mockReset();
  sched.listSchedules.mockResolvedValue({ items: [base, stopped, paused], next_before: null });
});
afterEach(() => cleanup());

describe("WorkflowSchedules", () => {
  it("lists schedules with Target, Schedule ID, Cron and a Stopped reason", async () => {
    render(<WorkflowSchedules workflowId="w1" target="Triage" />);
    expect(await screen.findByText("s1")).toBeInTheDocument();
    for (const header of ["Target", "Schedule ID", "Cron", "Next run", "State"]) expect(screen.getByRole("columnheader", { name: new RegExp(header) })).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.getByText(/Stopped: input_schema_errors: topic is required/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume Nightly" })).toBeInTheDocument();
  });

  it("the global page lists every workflow's schedules", async () => {
    render(<WorkflowSchedules />);
    await screen.findByText("s1");
    expect(sched.listSchedules).toHaveBeenCalledWith(expect.objectContaining({ workflowId: undefined }));
  });

  it("Pause patches active=false; Resume patches active=true", async () => {
    sched.updateSchedule.mockImplementation(async (_id: string, patch: object) => ({ ...base, ...patch }));
    render(<WorkflowSchedules workflowId="w1" target="Triage" />);
    fireEvent.click(await screen.findByRole("button", { name: "Pause Hourly" }));
    await waitFor(() => expect(sched.updateSchedule).toHaveBeenCalledWith("s1", { active: false }));
    fireEvent.click(screen.getByRole("button", { name: "Resume Nightly" }));
    await waitFor(() => expect(sched.updateSchedule).toHaveBeenCalledWith("s2", { active: true }));
  });

  it("Resume that the API refuses keeps the row stopped and shows the reason", async () => {
    sched.updateSchedule.mockRejectedValue(new Error("input_schema_errors"));
    render(<WorkflowSchedules workflowId="w1" target="Triage" />);
    fireEvent.click(await screen.findByRole("button", { name: "Resume Nightly" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("input_schema_errors");
    expect(screen.getByText(/Stopped: input_schema_errors: topic/)).toBeInTheDocument();
  });

  it("Delete asks first, then deletes", async () => {
    sched.deleteSchedule.mockResolvedValue(undefined);
    render(<WorkflowSchedules workflowId="w1" target="Triage" />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete Hourly" }));
    expect(sched.deleteSchedule).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Delete schedule" }));
    await waitFor(() => expect(sched.deleteSchedule).toHaveBeenCalledWith("s1"));
    await waitFor(() => expect(screen.queryByText("Hourly")).toBeNull());
  });

  it("the Schedules tab shows a count when schedules exist", () => {
    const withCount = workflowTabs("w1", "schedules", 3).find((t) => t.testId === "workflow-tab-schedules");
    expect(withCount?.count).toBe(3);
    expect(workflowTabs("w1", "schedules", 0).find((t) => t.testId === "workflow-tab-schedules")?.count).toBeUndefined();
    expect(workflowTabs("w1", "schedules").find((t) => t.testId === "workflow-tab-schedules")?.count).toBeUndefined();
  });

  it("a failed Pause names the schedule row in its error", async () => {
    sched.updateSchedule.mockRejectedValue(new Error("Cron must have 5 fields"));
    render(<WorkflowSchedules workflowId="w1" target="Triage" />);
    fireEvent.click(await screen.findByRole("button", { name: "Pause Hourly" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Hourly: Cron must have 5 fields");
  });
});
