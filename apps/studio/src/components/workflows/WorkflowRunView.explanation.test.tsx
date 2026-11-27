import React from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { SubscribeHandlers } from "@/lib/runs";
import { demoSpec } from "@/test/workflowFixtures";

const runsMock = vi.hoisted(() => ({ getRun: vi.fn(), listEvents: vi.fn(), subscribeRun: vi.fn() }));
const wfMock = vi.hoisted(() => ({ getWorkflowVersion: vi.fn(), decideApproval: vi.fn() }));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { WorkflowRunView } from "./WorkflowRunView";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const EXPLANATION = { workflow_id: "wf-1", version: 1, locale: "zh-TW", model: "m", created_at: "2026-11-20T10:00:00Z", summary: "s", inputs: [], outcomes: [], steps: [{ id: "classify", note: "把來信分類。" }] };
const RUN = {
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 1, agent_id: null, version: null,
  status: "running", display_status: "running", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-13T10:00:00Z", updated_at: "2026-11-13T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
};
let cached = false;
let methods: string[];

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});
beforeEach(() => {
  methods = [];
  runsMock.getRun.mockResolvedValue(RUN);
  runsMock.listEvents.mockResolvedValue([]);
  runsMock.subscribeRun.mockImplementation((_id: string, _h: SubscribeHandlers) => () => undefined);
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: demoSpec, spec_hash: "h", created_at: "", created_by: "u" });
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal("navigator", { ...navigator, language: "zh-TW" });
  vi.stubGlobal("fetch", vi.fn(async (_u: unknown, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    methods.push(method);
    if (method === "POST") return json(EXPLANATION);
    return cached ? json(EXPLANATION) : json({ detail: "none", code: "explanation_missing" }, 404);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

async function openClassify() {
  render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
  await screen.findByTestId("workflow-step-classify");
  fireEvent.click(within(screen.getByTestId("workflow-step-classify")).getByRole("button", { name: /^Step / }));
  return screen.getByTestId("step-io-panel");
}

describe("WorkflowRunView step 說明", () => {
  it("shows the cached note in the step panel", async () => {
    cached = true;
    const panel = await openClassify();
    expect(await within(panel).findByTestId("step-explanation-note")).toHaveTextContent("把來信分類。");
    expect(methods).not.toContain("POST");
  });

  it("offers 產生說明 when missing; one POST on click, then the note", async () => {
    cached = false;
    const panel = await openClassify();
    const button = await within(panel).findByTestId("step-explanation-generate");
    expect(methods).not.toContain("POST");
    fireEvent.click(button);
    expect(await within(panel).findByTestId("step-explanation-note")).toHaveTextContent("把來信分類。");
    expect(methods.filter((m) => m === "POST")).toHaveLength(1);
  });
});
