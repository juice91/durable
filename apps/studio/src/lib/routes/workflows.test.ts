import { describe, expect, it } from "vitest";
import { RouteIdError, routes } from "./index";
import { collapsedParam, parseCollapsedParam, parseWorkflowTab } from "./workflows";

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

  it("carries collapsed loops (?collapsed=, LP.6) on the Graph tab and the run view, sorted", () => {
    const set = new Set(["inner", "each"]);
    expect(routes.workflow("wf-1", { collapsed: set })).toBe("/workflows/wf-1?collapsed=each%2Cinner");
    expect(routes.workflow("wf-1", { collapsed: new Set() })).toBe("/workflows/wf-1");
    // The Runs tab has no graph.
    expect(routes.workflow("wf-1", { tab: "runs", collapsed: set })).toBe("/workflows/wf-1?tab=runs");
    expect(routes.workflowRun("wf-1", "r1", { collapsed: set })).toBe("/workflows/wf-1/runs/r1?collapsed=each%2Cinner");
    expect(collapsedParam(["b", "a", "Not-An-Id"])).toBe("a,b");
    expect(collapsedParam(null)).toBeNull();
    expect([...parseCollapsedParam("each, inner,,x;y,BAD")]).toEqual(["each", "inner"]);
    expect(parseCollapsedParam(null).size).toBe(0);
  });
});
