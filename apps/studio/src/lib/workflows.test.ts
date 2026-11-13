import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { ToolRunError } from "./toolRuns";
import { toolRunLedger } from "./toolRunLedger";
import { decideApproval, listWorkflowRuns, startWorkflowRun, workflowRunLedger } from "./workflows";

const fetchMock = vi.fn();

function respond(status: number, body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("workflow API client", () => {
  it("starts a run with input, version and run_id", async () => {
    fetchMock.mockReturnValue(respond(202, { run_id: "r1", kind: "workflow", workflow_id: "w1", version: 2, status: "queued", display_status: "queued", trace_id: null }));
    const accepted = await startWorkflowRun("w1", { text: "hi" }, { version: 2, runId: "r1" });
    expect(accepted.run_id).toBe("r1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/api\/v1\/workflows\/w1\/runs$/);
    expect(JSON.parse(init.body)).toEqual({ input: { text: "hi" }, version: 2, run_id: "r1" });
  });

  it("maps a 422 to per-field errors and a string detail to a banner", async () => {
    fetchMock.mockReturnValueOnce(respond(422, { detail: [{ loc: ["body", "input", "text"], msg: "does not satisfy the input schema's 'type' keyword", type: "json_schema.type" }] }));
    const err = await startWorkflowRun("w1", {}).catch((e) => e);
    expect(err).toBeInstanceOf(ToolRunError);
    expect(err.fieldErrors).toEqual({ text: "does not satisfy the input schema's 'type' keyword" });
    fetchMock.mockReturnValueOnce(respond(409, { detail: "Workflow has no published version" }));
    const conflict = await startWorkflowRun("w1", {}).catch((e) => e);
    expect(conflict.banner).toBe("Workflow has no published version");
    fetchMock.mockReturnValueOnce(Promise.resolve(new Response("oops", { status: 404 })));
    expect((await startWorkflowRun("w1", {}).catch((e) => e)).banner).toBe("Workflow or version not found");
  });

  it("decides approvals with an optional trimmed comment and surfaces 409", async () => {
    fetchMock.mockReturnValueOnce(respond(200, { run_id: "r", step_id: "s", decision: "approved", decided_by: "u", comment: "ok", notified: true }));
    await decideApproval("r", "s", "approve", "  ok ");
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/api\/v1\/workflows\/runs\/r\/steps\/s\/approve$/);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ comment: "ok" });
    fetchMock.mockReturnValueOnce(respond(409, { detail: "step is not waiting for approval" }));
    const err = await decideApproval("r", "s", "reject").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({});
  });

  it("lists a workflow's runs with kind and workflow_id filters", async () => {
    fetchMock.mockReturnValue(respond(200, { items: [], next_before: null }));
    await listWorkflowRuns("w1");
    const url = new URL(fetchMock.mock.calls[0][0]);
    expect(url.pathname).toBe("/api/v1/runs");
    expect(url.searchParams.get("kind")).toBe("workflow");
    expect(url.searchParams.get("workflow_id")).toBe("w1");
  });
});

describe("workflowRunLedger", () => {
  it("is separate from the tool run ledger", () => {
    toolRunLedger.reset();
    workflowRunLedger.reset();
    workflowRunLedger.remember({ toolId: "w1", toolTitle: "W", inputKey: "k", runId: "r1", state: "uncertain" });
    expect(toolRunLedger.pending("w1", "k")).toBeUndefined();
    expect(window.sessionStorage.getItem("eap.studio.workflowRuns.v1")).toContain("r1");
    expect(window.sessionStorage.getItem("eap.studio.toolRuns.v1")).toBeNull();
    workflowRunLedger.reloadFromStorage();
    expect(workflowRunLedger.pending("w1", "k")?.runId).toBe("r1");
    workflowRunLedger.reset();
  });
});
