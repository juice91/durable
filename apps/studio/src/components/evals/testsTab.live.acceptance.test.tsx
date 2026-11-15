/**
 * AC.16 acceptance: an eval run's live progress over SSE (events, reconnect, completion) and its
 * results DataList with the per-evaluator Sheet, against a fake of the AC.12 API.
 */
import React from "react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { nextRouter } from "@/test/nextRouterMock";
import { routes } from "@/lib/routes";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { TestsTab } from "./TestsTab";
import { AGENT, RUN_A, SUITE, callsTo, installFakeApi, resultOf, runOf, type FakeApi } from "./evalsFakeApi";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

let fake: FakeApi;
beforeEach(() => {
  fake = installFakeApi();
});
afterEach(() => {
  cleanup();
  fake.restore();
});

function renderRun(extra = "") {
  nextRouter.reset([`${routes.agentTests(AGENT, { suite: SUITE, run: RUN_A })}${extra}`]);
  return render(
    <TooltipProvider>
      <TestsTab agentId={AGENT} />
    </TooltipProvider>,
  );
}
const second = () => resultOf({ case_id: "c2", case_name: "second", position: 1, child_run_id: "child-2" });
const event = (seq: number, type: string) => ({ seq, type, data: {}, ts: "2026-11-15T10:00:00Z" });
const SLOW = { timeout: 4000 };

describe("live progress", () => {
  const running = () => {
    fake.state.runs = [runOf(RUN_A, 2, "running")];
    fake.state.runs[0].aggregates = undefined;
    fake.state.runInputs[RUN_A] = JSON.stringify({ cases: [{ name: "first" }, { name: "second" }] });
    fake.state.results[RUN_A] = [];
  };
  const connected = async () => {
    await waitFor(() => expect(fake.stream(RUN_A)).not.toBeNull());
    return fake.stream(RUN_A)!;
  };

  it("counts finished cases from eval_case_result events, against the snapshot's case count", async () => {
    running();
    renderRun();
    await screen.findByTestId("eval-progress-count");
    await waitFor(() => expect(screen.getByTestId("eval-progress-count")).toHaveTextContent("0 of 2"));
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
    const stream = await connected();
    fake.state.results[RUN_A] = [resultOf()];
    await act(async () => stream.push("run_event", event(1, "eval_case_result"), 1));
    await waitFor(() => expect(screen.getByTestId("eval-progress-count")).toHaveTextContent("1 of 2"));
    expect(await screen.findAllByTestId("eval-result-row")).toHaveLength(1);
  });

  it("reconnects after the stream drops, resuming from the last event id", async () => {
    running();
    renderRun();
    const first = await connected();
    fake.state.results[RUN_A] = [resultOf()];
    await act(async () => first.push("run_event", event(4, "eval_case_result"), 4));
    await waitFor(() => expect(screen.getByTestId("eval-progress-count")).toHaveTextContent("1 of 2"));
    await act(async () => first.end());
    await waitFor(() => expect(fake.state.streams.filter((s) => s.runId === RUN_A)).toHaveLength(2), SLOW);
    const again = fake.stream(RUN_A)!;
    expect(again.headers["last-event-id"]).toBe("4");
    fake.state.results[RUN_A] = [resultOf(), second()];
    await act(async () => again.push("run_event", event(5, "eval_case_result"), 5));
    await waitFor(() => expect(screen.getByTestId("eval-progress-count")).toHaveTextContent("2 of 2"));
  });

  it("ignores a replayed event and refreshes on the first one only", async () => {
    running();
    renderRun();
    const stream = await connected();
    await screen.findByTestId("eval-progress-count");
    const before = callsTo(fake.state, "GET", /evals\/runs\/[^/]+\/results$/).length;
    await act(async () => {
      stream.push("run_event", event(1, "eval_case_result"), 1);
      stream.push("run_event", event(1, "eval_case_result"), 1);
    });
    await waitFor(() => expect(callsTo(fake.state, "GET", /evals\/runs\/[^/]+\/results$/).length).toBeGreaterThan(before));
    await act(async () => new Promise((r) => setTimeout(r, 50)));
    expect(callsTo(fake.state, "GET", /evals\/runs\/[^/]+\/results$/).length - before).toBeLessThanOrEqual(2);
  });

  it("shows the completed state on run_completed: progress gone, aggregates in, no further stream", async () => {
    running();
    renderRun();
    const stream = await connected();
    await screen.findByTestId("eval-progress-count");
    fake.state.runs = [runOf(RUN_A, 2, "completed", 0.5)];
    fake.state.results[RUN_A] = [resultOf(), second()];
    await act(async () => stream.push("run_event", event(9, "run_completed"), 9));
    expect(await screen.findByTestId("eval-kpi-pass-rate")).toHaveTextContent("50%");
    await waitFor(() => expect(screen.queryByTestId("eval-progress")).not.toBeInTheDocument());
    expect(screen.getAllByTestId("eval-result-row")).toHaveLength(2);
    await act(async () => new Promise((r) => setTimeout(r, 700)));
    expect(fake.state.streams.filter((s) => s.runId === RUN_A)).toHaveLength(1);
  });

  it("a terminal status frame also refreshes", async () => {
    running();
    renderRun();
    const stream = await connected();
    await screen.findByTestId("eval-progress-count");
    fake.state.runs = [runOf(RUN_A, 2, "completed")];
    fake.state.results[RUN_A] = [resultOf(), second()];
    await act(async () => stream.push("status", { status: "completed" }));
    expect(await screen.findByTestId("eval-kpi-passed")).toHaveTextContent("1 / 2");
  });

  it("a failed run shows its error and no progress bar; a finished run never opens a stream", async () => {
    fake.state.runs = [{ ...runOf(RUN_A, 2, "failed"), error: "worker crashed", aggregates: undefined }];
    renderRun();
    expect(await screen.findByTestId("eval-run-failed")).toHaveTextContent("worker crashed");
    expect(screen.queryByTestId("eval-progress")).not.toBeInTheDocument();
    expect(fake.state.streams).toHaveLength(0);
  });
});

describe("results", () => {
  const failed = resultOf({
    case_id: "c2",
    case_name: "second",
    position: 1,
    child_run_id: "child-2",
    status: "failed",
    output: { answer: "no" },
    assertions: { equals_expected: { value: false, reason: "got 'no'" }, tone: { value: true, reason: "Polite enough" } },
    scores: { tone: { value: 0.8, reason: "Polite enough" } },
    labels: { sentiment: { value: "neutral", reason: "Flat wording" } },
    evaluator_failures: [{ name: "schema", kind: "schema_valid", error: "schema could not be compiled" }],
  });
  const errored = resultOf({ case_id: "c3", case_name: "third", position: 2, child_run_id: null, status: "error", output: null, assertions: {}, error_code: "model_error", duration_ms: null, cost_usd: null });

  beforeEach(() => {
    fake.state.runs = [runOf(RUN_A, 2)];
    fake.state.results[RUN_A] = [resultOf(), failed, errored];
  });

  it("lists every case with its status, assertions and the reason it did not pass", async () => {
    renderRun();
    const rows = await screen.findAllByTestId("eval-result-row");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByTestId("eval-case-status")).toHaveAttribute("data-status", "passed");
    expect(within(rows[1]).getByTestId("eval-case-status")).toHaveAttribute("data-status", "failed");
    expect(rows[1]).toHaveTextContent("1 / 2");
    expect(within(rows[0]).getByTestId("eval-result-reason")).toHaveTextContent("—");
    expect(within(rows[1]).getByTestId("eval-result-reason")).toHaveTextContent("equals_expected: got 'no'");
    expect(within(rows[2]).getByTestId("eval-result-reason")).toHaveTextContent("Agent run failed: model_error");
    expect(screen.getByText("Avg llm_judge").parentElement).toHaveTextContent("0.5");
  });

  it("the Sheet shows assertions, scores, labels, evaluator failures, output and the case's trace", async () => {
    renderRun("&result=c2");
    const sheet = await screen.findByTestId("eval-result-sheet");
    const outcome = (section: string, name: string) => within(within(sheet).getByTestId(section)).getAllByTestId("eval-outcome").find((el) => el.getAttribute("data-name") === name)!;
    expect(outcome("eval-result-assertions", "equals_expected")).toHaveTextContent("Failed");
    expect(outcome("eval-result-assertions", "equals_expected")).toHaveTextContent("got 'no'");
    expect(outcome("eval-result-assertions", "tone")).toHaveTextContent("Passed");
    expect(outcome("eval-result-scores", "tone")).toHaveTextContent("0.8");
    expect(outcome("eval-result-scores", "tone")).toHaveTextContent("Polite enough");
    expect(outcome("eval-result-labels", "sentiment")).toHaveTextContent("neutral");
    expect(within(sheet).getByTestId("eval-result-failures")).toHaveTextContent("schema could not be compiled");
    expect(within(sheet).getByTestId("eval-result-why")).toHaveTextContent("equals_expected: got 'no'");
    expect(sheet).toHaveTextContent("answer");
    expect(within(sheet).getByTestId("eval-result-trace")).toHaveAttribute("href", expect.stringContaining("child-2"));
  });

  it("an errored case shows its error code and no trace link when it never ran", async () => {
    renderRun("&result=c3");
    const sheet = await screen.findByTestId("eval-result-sheet");
    expect(within(sheet).getByTestId("eval-result-error-code")).toHaveTextContent("model_error");
    expect(within(sheet).queryByTestId("eval-result-trace")).not.toBeInTheDocument();
    expect(within(sheet).queryByTestId("eval-result-assertions")).not.toBeInTheDocument();
  });

  it("follows next_cursor so every result is listed", async () => {
    fake.state.pageSize = 1;
    renderRun();
    await waitFor(() => expect(screen.getAllByTestId("eval-result-row")).toHaveLength(3));
    expect(callsTo(fake.state, "GET", /results$/).map((c) => c.query.get("after"))).toEqual([null, "1", "2"]);
  });

  it("searches cases by name", async () => {
    renderRun();
    await screen.findAllByTestId("eval-result-row");
    fireEvent.change(screen.getByPlaceholderText("Search cases"), { target: { value: "sec" } });
    await waitFor(() => expect(screen.getAllByTestId("eval-result-row")).toHaveLength(1));
  });

  it("a finished run without results says so; a run not finished says none has", async () => {
    fake.state.results[RUN_A] = [];
    renderRun();
    expect(await screen.findByText("No results")).toBeInTheDocument();
  });

  it("a load error offers Retry, which reloads the run", async () => {
    fake.fail({ method: "GET", path: /evals\/runs\/[^/]+$/, status: 500, body: { detail: "boom" }, once: true });
    renderRun();
    const error = await screen.findByTestId("eval-run-load-error");
    expect(error).toHaveTextContent("boom");
    fireEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("eval-kpi-pass-rate")).toBeInTheDocument();
  });
});
