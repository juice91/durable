import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ScoreTraceDialog } from "./ScoreTraceDialog";
import { listAgentScorers, listReusableScorers, listRunScores, scoreRun, ScoreRunError } from "@/lib/scorers";

vi.mock("@/lib/scorers", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/scorers")>()),
  listReusableScorers: vi.fn(),
  listAgentScorers: vi.fn(),
  listRunScores: vi.fn(),
  scoreRun: vi.fn(),
}));

const scorer = (id: string, name: string) => ({
  id, name, description: null, evaluator: { kind: "schema_valid" as const }, agent_ids: [], created_at: "", updated_at: "",
});
const attached = (id: string, name: string) => ({ id, name, evaluator: { kind: "schema_valid" as const }, sampling_rate: 1 });
const scored = (id: string) => ({ seq: 1, ts: "", scorer: "x", scorer_id: id, score: 1, passed: true, reason: null });

function setup(props: Partial<React.ComponentProps<typeof ScoreTraceDialog>> = {}) {
  const onRequested = vi.fn();
  const onOpenChange = vi.fn();
  render(<ScoreTraceDialog runId="r1" agentId="a1" open onOpenChange={onOpenChange} onRequested={onRequested} {...props} />);
  return { onRequested, onOpenChange };
}

describe("ScoreTraceDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listReusableScorers).mockResolvedValue([scorer("s1", "Valid JSON"), scorer("s2", "Polite"), scorer("s3", "Cheap")]);
    vi.mocked(listAgentScorers).mockResolvedValue([attached("s1", "Valid JSON"), attached("s2", "Polite")]);
    vi.mocked(listRunScores).mockResolvedValue([]);
    vi.mocked(scoreRun).mockResolvedValue(undefined);
  });

  it("lists the tenant's scorers with the attached ones preselected", async () => {
    setup();
    expect(await screen.findByRole("checkbox", { name: /Valid JSON/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Polite/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Cheap/ })).not.toBeChecked();
  });

  it("posts the chosen ids when Score is pressed and reports the request", async () => {
    const { onRequested, onOpenChange } = setup();
    fireEvent.click(await screen.findByRole("checkbox", { name: /Cheap/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Polite/ }));
    fireEvent.click(screen.getByRole("button", { name: "Score" }));
    await waitFor(() => expect(scoreRun).toHaveBeenCalledWith("r1", ["s1", "s3"]));
    await waitFor(() => expect(onRequested).toHaveBeenCalledWith({ count: 2, baseline: 0 }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("shows scorers that already scored the run as checked and disabled with Scored, and does not send them", async () => {
    vi.mocked(listRunScores).mockResolvedValue([scored("s1")]);
    setup();
    const done = await screen.findByRole("checkbox", { name: /Valid JSON/ });
    expect(done).toBeChecked();
    expect(done).toBeDisabled();
    expect(screen.getByText("Scored")).toBeInTheDocument();
    // Only scorers without a score event are preselected.
    expect(screen.getByRole("checkbox", { name: /Polite/ })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Score" }));
    await waitFor(() => expect(scoreRun).toHaveBeenCalledWith("r1", ["s2"]));
  });

  it("disables Score when every chosen scorer has already scored the run", async () => {
    vi.mocked(listRunScores).mockResolvedValue([scored("s1"), scored("s2")]);
    setup();
    await screen.findByRole("checkbox", { name: /Valid JSON/ });
    expect(screen.getByRole("button", { name: "Score" })).toBeDisabled();
  });

  it("explains a run_not_finished 409 readably and keeps the dialog open", async () => {
    vi.mocked(scoreRun).mockRejectedValue(new ScoreRunError("run_not_finished", "This run has not finished. Only completed runs can be scored."));
    const { onOpenChange } = setup();
    await screen.findByRole("checkbox", { name: /Valid JSON/ });
    fireEvent.click(screen.getByRole("button", { name: "Score" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Only completed runs can be scored");
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("explains an already_scored 409 readably", async () => {
    vi.mocked(scoreRun).mockRejectedValue(new ScoreRunError("already_scored", "Every selected scorer has already scored this run."));
    setup();
    await screen.findByRole("checkbox", { name: /Valid JSON/ });
    fireEvent.click(screen.getByRole("button", { name: "Score" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already scored");
  });

  it("without an agent (a workflow run) nothing is preselected", async () => {
    setup({ agentId: null });
    expect(await screen.findByRole("checkbox", { name: /Valid JSON/ })).not.toBeChecked();
    expect(listAgentScorers).not.toHaveBeenCalled();
  });

  it("still lists the scorers when the agent's attachments fail to load (nothing preselected)", async () => {
    vi.mocked(listAgentScorers).mockRejectedValue(new Error("boom"));
    setup();
    expect(await screen.findByRole("checkbox", { name: /Valid JSON/ })).not.toBeChecked();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
