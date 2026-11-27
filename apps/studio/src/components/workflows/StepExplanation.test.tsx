import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useWorkflowExplanation, type WorkflowExplanation } from "@/lib/workflowExplanations";
import { StepExplanation } from "./StepExplanation";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const EXPLANATION: WorkflowExplanation = {
  workflow_id: "w1", version: 2, locale: "zh-TW", model: "m", created_at: "2026-11-20T10:00:00Z", summary: "s",
  inputs: [], outcomes: [], steps: [{ id: "a", note: "檢查這個時段是否已經有貼文。" }],
};
let calls: string[];
let getReply: () => Response;
let postReply: () => Response;

function Harness({ version = 2, stepId = "a" }: { version?: number | null; stepId?: string }) {
  const control = useWorkflowExplanation("w1", version);
  return <StepExplanation control={control} stepId={stepId} published={version !== null} />;
}

beforeEach(() => {
  calls = [];
  getReply = () => json({ detail: "none", code: "explanation_missing" }, 404);
  postReply = () => json(EXPLANATION);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("navigator", { ...navigator, language: "zh-TW" });
  vi.stubGlobal("fetch", vi.fn(async (_input: unknown, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push(method);
    return method === "POST" ? postReply() : getReply();
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe("StepExplanation", () => {
  it("shows the cached note under 說明 without generating", async () => {
    getReply = () => json(EXPLANATION);
    render(<Harness />);
    expect(await screen.findByTestId("step-explanation-note")).toHaveTextContent("檢查這個時段是否已經有貼文。");
    expect(screen.getByTestId("step-explanation")).toHaveTextContent("說明");
    expect(calls).toEqual(["GET"]);
  });

  it("offers 產生說明 when missing, never auto-generates, then shows the note", async () => {
    render(<Harness />);
    const button = await screen.findByTestId("step-explanation-generate");
    expect(calls).toEqual(["GET"]);
    fireEvent.click(button);
    expect(await screen.findByTestId("step-explanation-note")).toHaveTextContent("檢查這個時段");
    expect(calls).toEqual(["GET", "POST"]);
  });

  it("says so when the version's explanation has no note for the step", async () => {
    getReply = () => json(EXPLANATION);
    render(<Harness stepId="zzz" />);
    expect(await screen.findByTestId("step-explanation-empty")).toBeInTheDocument();
  });

  it("shows an error with a retry", async () => {
    postReply = () => json({ detail: "did not answer in time" }, 504);
    render(<Harness />);
    fireEvent.click(await screen.findByTestId("step-explanation-generate"));
    expect(await screen.findByTestId("step-explanation-error")).toBeInTheDocument();
    postReply = () => json(EXPLANATION);
    fireEvent.click(screen.getByTestId("step-explanation-retry"));
    expect(await screen.findByTestId("step-explanation-note")).toBeInTheDocument();
  });

  it("a draft has no explanation", async () => {
    render(<Harness version={null} />);
    await waitFor(() => expect(screen.getByTestId("step-explanation-draft")).toHaveTextContent("發佈後可產生說明"));
    expect(calls).toEqual([]);
  });
});
