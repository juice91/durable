import React from "react";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const sched = vi.hoisted(() => ({ listSchedules: vi.fn() }));
vi.mock("@/lib/schedules", () => sched);
vi.mock("@/lib/workflows", () => ({ listWorkflows: vi.fn(async () => []) }));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("next/navigation", () => ({ usePathname: () => "/workflows/w1/schedules", useParams: () => ({}), useRouter: () => ({ push: vi.fn() }), useSearchParams: () => new URLSearchParams() }));

import { WorkflowHeader } from "./WorkflowHeader";

afterEach(() => {
  cleanup();
  sched.listSchedules.mockReset();
});

describe("WorkflowHeader schedule count", () => {
  it("does not fetch the count on the Schedules tab (the page owns it)", async () => {
    sched.listSchedules.mockResolvedValue({ items: [], next_before: null });
    render(<WorkflowHeader workflowId="w1" name="Triage" tab="schedules" />);
    await new Promise((r) => setTimeout(r, 20));
    expect(sched.listSchedules).not.toHaveBeenCalled();
  });

  it("fetches it once on the other tabs", async () => {
    sched.listSchedules.mockResolvedValue({ items: [{ id: "s1" }], next_before: null });
    render(<WorkflowHeader workflowId="w1" name="Triage" tab="runs" />);
    await waitFor(() => expect(sched.listSchedules).toHaveBeenCalledTimes(1));
  });
});
