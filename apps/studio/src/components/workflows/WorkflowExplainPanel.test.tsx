import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { explanationLocale, useWorkflowExplanation, type WorkflowExplanation } from "@/lib/workflowExplanations";
import type { WorkflowSpec } from "@/lib/workflows";
import { StepIoPanel } from "./StepIoPanel";
import { WorkflowExplainPanel } from "./WorkflowExplainPanel";
import { WorkflowStepList } from "./WorkflowGraph";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const EXPLANATION: WorkflowExplanation = {
  workflow_id: "w1",
  version: 2,
  locale: "zh-TW",
  model: "litellm:qwen3.8",
  created_at: "2026-11-20T10:00:00Z",
  summary: "每個時段先確認是否已經發過文章，已發過時不做任何事。",
  inputs: [{ name: "slot", meaning: "這次要處理的時段" }],
  outcomes: [{ status: "skipped", meaning: "這個時段已經發過了" }],
  steps: [{ id: "posted_gate", note: "檢查這個時段是否已經有貼文。" }, { id: "finish", note: "回傳結果。" }],
};

const SPEC = {
  name: "Post",
  input_schema: { type: "object", properties: {} },
  start: "posted_gate",
  steps: [
    { id: "posted_gate", type: "tool", tool_id: "clock.now", input: {}, next: "finish" },
    { id: "finish", type: "tool", tool_id: "workflow.return", input: { value: 1 } },
  ],
} as unknown as WorkflowSpec;

let calls: { method: string; url: string; body: any }[];
let getReply: () => Response;
let postReply: () => Response;

/** The page's wiring in miniature: the hook feeds the panel and the step list. */
function Harness({ version = 2 }: { version?: number | null }) {
  const control = useWorkflowExplanation("w1", version);
  return (
    <div>
      <WorkflowExplainPanel control={control} published={version !== null} onClose={() => undefined} />
      <WorkflowStepList spec={SPEC} notes={control.notes} />
    </div>
  );
}

beforeEach(() => {
  calls = [];
  getReply = () => json({ detail: "none", code: "explanation_missing" }, 404);
  postReply = () => json(EXPLANATION);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("navigator", { ...navigator, language: "zh-TW" });
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ method, url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
    return method === "POST" ? postReply() : getReply();
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("explanationLocale", () => {
  it("is zh-TW for a zh browser and en otherwise", () => {
    expect(explanationLocale("zh-TW")).toBe("zh-TW");
    expect(explanationLocale("zh-Hans-CN")).toBe("zh-TW");
    expect(explanationLocale("en-US")).toBe("en");
    expect(explanationLocale("fr")).toBe("en");
    expect(explanationLocale(null)).toBe("en");
  });
});

describe("WorkflowExplainPanel", () => {
  it("offers to generate when nothing is cached, shows the spinner, then the content", async () => {
    let release: () => void = () => undefined;
    postReply = () => json(EXPLANATION);
    const slow = vi.mocked(fetch);
    render(<Harness />);
    const generate = await screen.findByTestId("workflow-explain-generate");
    expect(calls[0]).toMatchObject({ method: "GET", url: expect.stringContaining("/workflows/w1/explanation?version=2&locale=zh-TW") });
    slow.mockImplementationOnce((_input, init) => {
      calls.push({ method: init?.method ?? "GET", url: String(_input), body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Promise<Response>((resolve) => { release = () => resolve(json(EXPLANATION)); });
    });
    fireEvent.click(generate);
    expect(await screen.findByTestId("workflow-explain-loading")).toHaveTextContent("正在產生…");
    release();
    expect(await screen.findByTestId("workflow-explain-summary")).toHaveTextContent("每個時段先確認");
    expect(calls.at(-1)).toMatchObject({ method: "POST", body: { version: 2, locale: "zh-TW", refresh: false } });
  });

  it("renders a cached explanation: summary, inputs, endings, steps, model and time, and a refresh link", async () => {
    getReply = () => json(EXPLANATION);
    render(<Harness />);
    expect(await screen.findByTestId("workflow-explain-summary")).toHaveTextContent("已發過時不做任何事");
    expect(within(screen.getByTestId("workflow-explain-inputs")).getByText("slot")).toBeInTheDocument();
    expect(within(screen.getByTestId("workflow-explain-outcomes")).getByText("這個時段已經發過了")).toBeInTheDocument();
    const steps = screen.getAllByTestId("workflow-explain-step");
    expect(steps).toHaveLength(2);
    expect(steps[0]).toHaveTextContent("posted_gate");
    expect(steps[0]).toHaveTextContent("檢查這個時段是否已經有貼文。");
    expect(screen.getByTestId("workflow-explain-meta")).toHaveTextContent("litellm:qwen3.8");
    expect(screen.queryByTestId("workflow-explain-generate")).toBeNull();

    postReply = () => json({ ...EXPLANATION, summary: "新的說明。" });
    fireEvent.click(screen.getByTestId("workflow-explain-refresh"));
    expect(await screen.findByText("新的說明。")).toBeInTheDocument();
    expect(calls.at(-1)?.body).toMatchObject({ refresh: true });
  });

  it("shows the step notes under the steps of the list, labelled AI 註解, only when cached", async () => {
    getReply = () => json(EXPLANATION);
    const { unmount } = render(<Harness />);
    const note = await screen.findByTestId("workflow-step-ai-note-posted_gate");
    expect(note).toHaveTextContent("AI 註解");
    expect(note).toHaveTextContent("檢查這個時段是否已經有貼文。");
    unmount();
    getReply = () => json({ detail: "none", code: "explanation_missing" }, 404);
    render(<Harness />);
    await screen.findByTestId("workflow-explain-generate");
    expect(screen.queryByTestId("workflow-step-ai-note-posted_gate")).toBeNull();
  });

  it("shows the error with a retry that generates again", async () => {
    postReply = () => json({ detail: "The workflow explainer did not answer in time; try again.", code: "explanation_timeout" }, 504);
    render(<Harness />);
    fireEvent.click(await screen.findByTestId("workflow-explain-generate"));
    expect(await screen.findByTestId("workflow-explain-error")).toHaveTextContent("did not answer in time");
    postReply = () => json(EXPLANATION);
    fireEvent.click(screen.getByTestId("workflow-explain-retry"));
    expect(await screen.findByTestId("workflow-explain-summary")).toBeInTheDocument();
  });

  it("names the missing model on a 409 no_model", async () => {
    postReply = () => json({ detail: "x", code: "no_model" }, 409);
    render(<Harness />);
    fireEvent.click(await screen.findByTestId("workflow-explain-generate"));
    expect(await screen.findByTestId("workflow-explain-error")).toHaveTextContent("No model is available");
  });

  it("says a draft can be explained after publishing and never calls the API", async () => {
    render(<Harness version={null} />);
    expect(await screen.findByTestId("workflow-explain-draft")).toHaveTextContent("發佈後可產生說明");
    expect(calls).toHaveLength(0);
  });

  it("asks for English on a non-Chinese browser", async () => {
    vi.stubGlobal("navigator", { ...navigator, language: "en-US" });
    render(<Harness />);
    await screen.findByTestId("workflow-explain-generate");
    expect(calls[0].url).toContain("locale=en");
  });

  it("has no axe violations with content", async () => {
    getReply = () => json(EXPLANATION);
    const { container } = render(<Harness />);
    await screen.findByTestId("workflow-explain-summary");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("StepIoPanel AI note", () => {
  const detail = { stepId: "posted_gate", state: "completed", stepType: "tool", started: true, hasOutput: false, error: null, errorCode: null, childRunId: null, agentId: null, toolId: null, prompt: null, deadline: null, timeoutHours: null, decision: null, skipReason: null, startedAt: null, endedAt: null } as never;

  it("shows the note under the AI 註解 label, and nothing without one", () => {
    const { rerender } = render(<StepIoPanel step={null} detail={detail} onClose={() => undefined} aiNote="檢查這個時段是否已經有貼文。" />);
    expect(screen.getByTestId("step-io-ai-note")).toHaveTextContent("AI 註解檢查這個時段是否已經有貼文。");
    rerender(<StepIoPanel step={null} detail={detail} onClose={() => undefined} />);
    expect(screen.queryByTestId("step-io-ai-note")).toBeNull();
  });
});
