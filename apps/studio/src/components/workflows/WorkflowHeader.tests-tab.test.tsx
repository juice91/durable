import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/schedules", () => ({ listSchedules: vi.fn(async () => ({ items: [], next_before: null })) }));
vi.mock("@/lib/workflows", () => ({ listWorkflows: vi.fn(async () => []) }));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("next/navigation", () => ({ usePathname: () => "/workflows/w1/tests", useParams: () => ({}), useRouter: () => ({ push: vi.fn() }), useSearchParams: () => new URLSearchParams() }));

import { WorkflowHeader } from "./WorkflowHeader";

afterEach(cleanup);

describe("WorkflowHeader Tests tab", () => {
  it("has Graph, Runs, Traces, Schedules and Tests, with Tests current on its page", () => {
    render(<WorkflowHeader workflowId="w1" name="Triage" tab="tests" />);
    const tests = screen.getByTestId("workflow-tab-tests");
    expect(tests).toHaveAttribute("href", "/workflows/w1/tests");
    expect(tests).toHaveAttribute("aria-current", "page");
    for (const id of ["graph", "runs", "traces", "schedules"]) expect(screen.getByTestId(`workflow-tab-${id}`)).not.toHaveAttribute("aria-current");
  });
});
