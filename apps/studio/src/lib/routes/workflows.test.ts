import { describe, expect, it } from "vitest";
import { RouteIdError, routes } from "./index";
import { parseWorkflowTab } from "./workflows";

describe("workflow routes (SL.14)", () => {
  it("builds the Graph, Runs and Traces tabs and keeps the run view URL", () => {
    expect(routes.workflow("wf-1")).toBe("/workflows/wf-1");
    expect(routes.workflow("wf-1", { tab: "graph" })).toBe("/workflows/wf-1");
    expect(routes.workflow("wf-1", { tab: "runs" })).toBe("/workflows/wf-1?tab=runs");
    expect(routes.workflowTraces("wf-1")).toBe("/workflows/wf-1/traces");
    expect(routes.workflowRun("wf-1", "r1")).toBe("/workflows/wf-1/runs/r1");
  });

  it("encodes ids and rejects dot segments", () => {
    expect(routes.workflowTraces("a b")).toBe("/workflows/a%20b/traces");
    expect(() => routes.workflowTraces("..")).toThrow(RouteIdError);
  });

  it("parses the tab parameter, defaulting to graph", () => {
    expect(parseWorkflowTab("runs")).toBe("runs");
    expect(parseWorkflowTab(null)).toBe("graph");
    expect(parseWorkflowTab("traces")).toBe("graph");
  });
});
