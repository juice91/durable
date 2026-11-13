import { describe, expect, it } from "vitest";
import { ROUTE_AREAS, RouteIdError, routes, withQuery } from "./index";

describe("routes: today's URLs", () => {
  it.each([
    [routes.dashboard(), "/"],
    [routes.agents(), "/agents"],
    [routes.agentNew(), "/agents/new"],
    [routes.agentEditor("a1"), "/agents/a1"],
    [routes.playground(), "/playground"],
    [routes.requestContext(), "/request-context"],
    [routes.workflows(), "/workflows"],
    [routes.workflow("wf-1"), "/workflows/wf-1"],
    [routes.workflowRun("wf-1", "r1"), "/workflows/wf-1/runs/r1"],
    [routes.tools(), "/tools"],
    [routes.mcp(), "/mcp"],
    [routes.processors(), "/processors"],
    [routes.traces(), "/traces"],
    [routes.trace("r1"), "/traces/r1"],
    [routes.run("r1"), "/runs/r1"],
    [routes.logs(), "/logs"],
    [routes.metrics(), "/metrics"],
    [routes.settings(), "/settings"],
  ])("%s is %s", (actual, expected) => {
    expect(actual).toBe(expected);
  });
});

describe("routes: areas", () => {
  it("builder names are unique across areas, so no spread silently replaces another", () => {
    const names = Object.values(ROUTE_AREAS).flatMap((area) => Object.keys(area));
    const dupes = names.filter((n, i) => names.indexOf(n) !== i);
    expect(dupes).toEqual([]);
    expect(Object.keys(routes).sort()).toEqual([...names].sort());
  });
});

describe("routes: encoding", () => {
  it("encodes id segments, including ':' '/' '?' '#' and spaces", () => {
    expect(routes.agentEditor("ns:agent/1")).toBe("/agents/ns%3Aagent%2F1");
    expect(routes.trace("run:1?x#y")).toBe("/traces/run%3A1%3Fx%23y");
    expect(routes.run("a b")).toBe("/runs/a%20b");
    expect(routes.workflow("wf:1")).toBe("/workflows/wf%3A1");
    expect(routes.workflowRun("wf/1", "run:2")).toBe("/workflows/wf%2F1/runs/run%3A2");
  });

  it("throws RouteIdError for '.' and '..' ids in every segment builder", () => {
    for (const id of [".", ".."]) {
      expect(() => routes.agentEditor(id)).toThrow(RouteIdError);
      expect(() => routes.trace(id)).toThrow("route id cannot be '.' or '..'");
      expect(() => routes.run(id)).toThrow(RouteIdError);
      expect(() => routes.workflow(id)).toThrow(RouteIdError);
      expect(() => routes.workflowRun("wf", id)).toThrow(RouteIdError);
    }
    expect(routes.agentEditor("...")).toBe("/agents/...");
    expect(routes.agentEditor(".a")).toBe("/agents/.a");
  });

  it("round-trips an encoded segment", () => {
    const id = "tenant:agent/π 1";
    expect(decodeURIComponent(routes.agentEditor(id).split("/")[2])).toBe(id);
  });

  it("builds playground queries in a fixed key order, leaving out empty options", () => {
    expect(routes.playground({ agent: "a1", conversation: "c1" })).toBe("/playground?agent=a1&conversation=c1");
    expect(routes.playground({ context: "preset", model: "openai:gpt-4.1", version: 3, conversation: "c", agent: "a" })).toBe(
      "/playground?agent=a&conversation=c&version=3&model=openai%3Agpt-4.1&context=preset",
    );
    expect(routes.playground({ agent: "a 1", version: null, model: "", context: null })).toBe("/playground?agent=a+1");
    expect(routes.playground({ agent: "a1", version: "draft" })).toBe("/playground?agent=a1&version=draft");
    expect(routes.playground({ agent: "a1", version: 0 })).toBe("/playground?agent=a1&version=0");
  });

  it("builds request-context queries", () => {
    expect(routes.requestContext({ agent: "a&b", version: 2 })).toBe("/request-context?agent=a%26b&version=2");
    expect(routes.requestContext({ agent: null })).toBe("/request-context");
  });

  it("takes URLSearchParams or a record for list pages", () => {
    expect(routes.traces(new URLSearchParams("status=failed,needs_attention&model=openai:gpt-4.1"))).toBe(
      "/traces?status=failed%2Cneeds_attention&model=openai%3Agpt-4.1",
    );
    expect(routes.traces(new URLSearchParams())).toBe("/traces");
    expect(routes.logs({ q: "a b", level: undefined, run_id: "r1" })).toBe("/logs?q=a+b&run_id=r1");
    expect(routes.metrics({ range: "30d", agent_id: null })).toBe("/metrics?range=30d");
    expect(routes.dashboard({ range: "30d" })).toBe("/?range=30d");
    expect(routes.dashboard({})).toBe("/");
  });

  it("withQuery leaves out null, undefined, false and empty strings but keeps 0 and true", () => {
    expect(withQuery("/x", { a: null, b: undefined, c: false, d: "", e: 0, f: true })).toBe("/x?e=0&f=true");
    expect(withQuery("/x")).toBe("/x");
  });
});
