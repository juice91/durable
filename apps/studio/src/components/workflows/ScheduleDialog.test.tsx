import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A rejecting vi.fn() is reported as an unhandled rejection by this vitest version, so the failure
// case goes through a plain function behind the spy.
const sched = vi.hoisted(() => ({ createSchedule: vi.fn(), failWith: null as Error | null }));
vi.mock("@/lib/schedules", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createSchedule: (...args: unknown[]) => (sched.failWith ? Promise.reject(sched.failWith) : sched.createSchedule(...args)),
}));

import { ScheduleDialog } from "./ScheduleDialog";
import type { WorkflowSpec } from "@/lib/workflows";

const spec = {
  spec_version: 1, name: "Triage", start: "a", steps: [],
  input_schema: { type: "object", required: ["topic"], properties: { topic: { type: "string" } } },
} as unknown as WorkflowSpec;

beforeEach(() => {
  sched.createSchedule.mockReset();
  sched.failWith = null;
});
afterEach(() => cleanup());

function open(onSaved = vi.fn(), onOpenChange = vi.fn()) {
  render(<ScheduleDialog workflowId="w1" spec={spec} open onOpenChange={onOpenChange} onSaved={onSaved} />);
  return { onSaved, onOpenChange };
}

describe("ScheduleDialog", () => {
  it("defaults the timezone from the browser and previews the cron live", () => {
    open();
    const expected = Intl.DateTimeFormat().resolvedOptions().timeZone;
    expect(screen.getByLabelText("Timezone")).toHaveValue(expected);
    fireEvent.change(screen.getByLabelText("Cron"), { target: { value: "*/5 * * * *" } });
    expect(screen.getByTestId("schedule-cron-preview")).toHaveTextContent("Every 5 minutes");
  });

  it("New schedule posts name, cron, timezone and input", async () => {
    sched.createSchedule.mockResolvedValue({ id: "s9" });
    const { onSaved, onOpenChange } = open();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Hourly" } });
    fireEvent.change(screen.getByLabelText("Cron"), { target: { value: "5 * * * *" } });
    fireEvent.change(screen.getByLabelText("Timezone"), { target: { value: "Asia/Taipei" } });
    fireEvent.change(screen.getByLabelText(/Topic/), { target: { value: "billing" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    await waitFor(() => expect(sched.createSchedule).toHaveBeenCalledWith("w1", { name: "Hourly", cron: "5 * * * *", timezone: "Asia/Taipei", input: { topic: "billing" } }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ id: "s9" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("shows the API's reason and keeps the dialog open", async () => {
    sched.failWith = new Error("invalid_cron: cron must be 5 fields");
    const { onSaved } = open();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Bad" } });
    fireEvent.change(screen.getByLabelText("Cron"), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid_cron: cron must be 5 fields");
    expect(onSaved).not.toHaveBeenCalled();
  });
});
