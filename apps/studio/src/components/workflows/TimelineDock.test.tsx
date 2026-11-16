// Task 21: a step row of the Timeline dock downloads the step's input and output as JSON files.
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TimelineDock } from "./TimelineDock";
import type { TimelineRow } from "./workflowTimeline";

const row = (stepId: string, extra: Partial<TimelineRow> = {}): TimelineRow => ({
  key: stepId, kind: "step", stepId, stepType: "agent", state: "completed", startedAt: "2026-11-16T10:00:00Z", endedAt: "2026-11-16T10:00:02Z",
  durationMs: 2000, bar: { offset: 0, width: 1 }, parentId: null, depth: 0, ...extra,
});

function renderDock(rows: TimelineRow[]) {
  return render(
    <TimelineDock runId="run-9" rows={rows} open height={200} onOpenChange={vi.fn()} onHeightChange={vi.fn()} selectedStepId={null} onSelectStep={vi.fn()} />,
  );
}

describe("TimelineDock downloads", () => {
  const saved: Array<{ name: string; blob: Blob }> = [];
  beforeEach(() => {
    saved.length = 0;
    let pending: Blob | null = null;
    Object.assign(URL, { createObjectURL: vi.fn((blob: Blob) => ((pending = blob), "blob:x")), revokeObjectURL: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ name: this.download, blob: pending as Blob });
    });
  });
  afterEach(() => vi.restoreAllMocks());

  const read = (blob: Blob) => new Promise<unknown>((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(JSON.parse(String(reader.result)));
    reader.readAsText(blob);
  });

  it("each step row has Download input and Download output producing <run>-<step>-input.json and -output.json", async () => {
    renderDock([row("classify", { input: { text: "hi" }, hasOutput: true, output: { label: "a" } })]);
    const item = screen.getByTestId("workflow-timeline-row-classify");
    fireEvent.click(within(item).getByRole("button", { name: "Download input" }));
    fireEvent.click(within(item).getByRole("button", { name: "Download output" }));
    expect(saved.map((s) => s.name)).toEqual(["run-9-classify-input.json", "run-9-classify-output.json"]);
    expect(await read(saved[0].blob)).toEqual({ text: "hi" });
    expect(await read(saved[1].blob)).toEqual({ label: "a" });
  });

  it("a download does not select the step", () => {
    const onSelectStep = vi.fn();
    render(<TimelineDock runId="run-9" rows={[row("a", { input: 1, hasOutput: true, output: 2 })]} open height={200} onOpenChange={vi.fn()} onHeightChange={vi.fn()} selectedStepId={null} onSelectStep={onSelectStep} />);
    fireEvent.click(screen.getByRole("button", { name: "Download input" }));
    expect(onSelectStep).not.toHaveBeenCalled();
  });

  it("disables a download the step has no data for", () => {
    renderDock([row("pending_step", { state: "pending", startedAt: null, endedAt: null, durationMs: null, bar: null })]);
    expect(screen.getByRole("button", { name: "Download input" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Download output" })).toBeDisabled();
  });

  it("iteration rows have no downloads", () => {
    renderDock([{ ...row("each"), key: "iteration:each.0", kind: "iteration", stepType: null, iteration: { loopId: "each", index: 0, scope: "each.0", shown: true, errorCode: null, pagerKey: "k" } }]);
    expect(screen.queryByRole("button", { name: "Download input" })).not.toBeInTheDocument();
  });
});
