/**
 * AC.16 acceptance: run history, compare (case changes, deltas, the suite warning) and "View spec
 * diff" into the Editor's CompareSheet, against a fake of the AC.12 API.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { nextRouter } from "@/test/nextRouterMock";
import { routes } from "@/lib/routes";
import type { EvalCompare } from "@/lib/evals";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { FrameHarness, mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH } from "@/components/frame/frameTesting";
import { EditorTab } from "@/components/agentPage/EditorTab";
import { TestsTab } from "./TestsTab";
import { AGENT, RUN_A, RUN_B, SUITE, callsTo, installFakeApi, resultOf, runOf, type FakeApi } from "./evalsFakeApi";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

let fake: FakeApi;
beforeEach(() => {
  fake = installFakeApi();
});
afterEach(() => {
  cleanup();
  fake.restore();
});

function renderAt(href: string) {
  nextRouter.reset([href]);
  return render(
    <TooltipProvider>
      <TestsTab agentId={AGENT} />
    </TooltipProvider>,
  );
}
const side = (id: string, version: number | null, draft: boolean, passRate: number, suiteHash = "h") => ({
  run_id: id, agent_id: AGENT, agent_version: version, draft, suite_id: SUITE, suite_hash: suiteHash, status: "completed",
  aggregates: runOf(id, version, "completed", passRate).aggregates!,
});
const compareOf = (extra: Partial<EvalCompare> = {}): EvalCompare => ({
  base: side(RUN_A, 1, false, 0.5),
  target: side(RUN_B, 2, false, 1),
  same_suite: true,
  warnings: [],
  summary: { fixed: 1, regressed: 1, unchanged: 1, added: 1, removed: 1, pass_rate_delta: 0.5, scores_avg_delta: { llm_judge: -0.25 }, cost_usd_delta: "0.0030", duration_ms_p50_delta: -40 },
  cases: [
    { name: "a", base_case_id: "c1", target_case_id: "c1", base_status: "failed", target_status: "passed", change: "fixed", assertions: { equals_expected: { base: false, target: true } }, score_deltas: { llm_judge: 0.5 }, duration_ms_delta: -10, cost_usd_delta: "-0.0001" },
    { name: "b", base_case_id: "c2", target_case_id: "c2", base_status: "passed", target_status: "failed", change: "regressed", assertions: {}, score_deltas: {}, duration_ms_delta: 5, cost_usd_delta: "0.0001" },
    { name: "c", base_case_id: "c3", target_case_id: "c3", base_status: "passed", target_status: "passed", change: "unchanged", assertions: {}, score_deltas: {}, duration_ms_delta: 0, cost_usd_delta: "0" },
    { name: "d", base_case_id: null, target_case_id: "c4", base_status: null, target_status: "passed", change: "added", assertions: {}, score_deltas: {}, duration_ms_delta: null, cost_usd_delta: null },
    { name: "e", base_case_id: "c5", target_case_id: null, base_status: "failed", target_status: null, change: "removed", assertions: {}, score_deltas: {}, duration_ms_delta: null, cost_usd_delta: null },
  ],
  base_version: 1,
  target_version: 2,
  ...extra,
});
const compareHref = (a = RUN_A, b = RUN_B) => `${routes.agentTests(AGENT, { suite: SUITE })}&compare=${a}..${b}`;

describe("run history", () => {
  beforeEach(() => {
    fake.state.runs = [runOf(RUN_B, 2, "completed", 1), runOf(RUN_A, 1, "completed", 0.5), { ...runOf("other", 2), suite_id: "s9" }];
  });

  it("lists this suite's runs only, newest first, each linking to its run", async () => {
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    const rows = await screen.findAllByTestId("eval-history-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("v2");
    expect(rows[0]).toHaveTextContent("100%");
    expect(rows[1]).toHaveTextContent("50%");
    expect(callsTo(fake.state, "GET", /evals\/runs$/)[0].query.get("limit")).toBe("50");
  });

  it("labels a draft run (version null) as Draft", async () => {
    fake.state.runs = [runOf(RUN_B, null), runOf(RUN_A, 1)];
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    expect((await screen.findAllByTestId("eval-history-row"))[0]).toHaveTextContent("Draft");
  });

  it("Compare stays disabled until two different runs are chosen", async () => {
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    const open = await screen.findByTestId("eval-compare-open");
    expect(open).toBeDisabled();
    fireEvent.change(screen.getByTestId("eval-compare-base"), { target: { value: RUN_A } });
    fireEvent.change(screen.getByTestId("eval-compare-target"), { target: { value: RUN_A } });
    expect(open).toBeDisabled();
    fireEvent.change(screen.getByTestId("eval-compare-target"), { target: { value: RUN_B } });
    expect(open).toBeEnabled();
    fireEvent.click(open);
    await waitFor(() => expect(nextRouter.url).toBe(compareHref()));
  });

  it("one run alone offers no compare", async () => {
    fake.state.runs = [runOf(RUN_A, 1)];
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    await screen.findAllByTestId("eval-history-row");
    expect(screen.queryByTestId("eval-compare-open")).not.toBeInTheDocument();
  });

  it("an open run compares with another: the chosen run is the base, this run the target", async () => {
    fake.state.results[RUN_B] = [resultOf()];
    renderAt(routes.agentTests(AGENT, { suite: SUITE, run: RUN_B }));
    const select = await screen.findByTestId("eval-compare-with");
    expect(within(select).getAllByRole("option")).toHaveLength(2);
    expect(screen.getByTestId("eval-compare-go")).toBeDisabled();
    fireEvent.change(select, { target: { value: RUN_A } });
    fireEvent.click(screen.getByTestId("eval-compare-go"));
    await waitFor(() => expect(nextRouter.url).toBe(compareHref(RUN_A, RUN_B)));
  });
});

describe("compare", () => {
  it("classifies every case and sends base and target to the API", async () => {
    fake.state.compare = compareOf();
    renderAt(compareHref());
    await waitFor(() => expect(screen.getByTestId("eval-compare-title")).toHaveTextContent("v1 → v2"));
    const rows = await screen.findAllByTestId("eval-compare-row");
    expect(rows.map((r) => within(r).getByTestId("eval-compare-change").getAttribute("data-change"))).toEqual(["fixed", "regressed", "unchanged", "added", "removed"]);
    const call = callsTo(fake.state, "GET", /evals\/compare$/)[0];
    expect([call.query.get("base"), call.query.get("target")]).toEqual([RUN_A, RUN_B]);
    expect(screen.getByTestId("eval-compare-counts")).toHaveTextContent("1 · 1");
    expect(rows[0]).toHaveTextContent("equals_expected: fail → pass");
  });

  it("colours deltas: pass rate up is better, cost up is worse, duration down is better", async () => {
    fake.state.compare = compareOf();
    renderAt(compareHref());
    expect(await screen.findByTestId("eval-compare-pass-rate-delta")).toHaveTextContent("+50 pts");
    expect(screen.getByTestId("eval-compare-pass-rate-delta")).toHaveAttribute("data-tone", "better");
    expect(screen.getByTestId("eval-compare-cost-delta")).toHaveAttribute("data-tone", "worse");
    expect(screen.getByTestId("eval-compare-cost-delta")).toHaveTextContent("+$0.0030");
    expect(within(screen.getByTestId("eval-compare-p50")).getByText(/40/).closest("[data-tone]")).toHaveAttribute("data-tone", "better");
    expect(within(screen.getByText("Avg llm_judge").parentElement!).getByText(/0\.25/).closest("[data-tone]")).toHaveAttribute("data-tone", "worse");
  });

  it("a zero or absent delta is neutral", async () => {
    fake.state.compare = compareOf({ summary: { ...compareOf().summary, pass_rate_delta: 0, cost_usd_delta: null } });
    renderAt(compareHref());
    expect(await screen.findByTestId("eval-compare-pass-rate-delta")).toHaveAttribute("data-tone", "same");
    expect(screen.getByTestId("eval-compare-cost-delta")).toHaveAttribute("data-tone", "same");
  });

  it("warns when the runs used different suites", async () => {
    fake.state.compare = compareOf({ same_suite: false, warnings: ["The runs used different test suites (their suite hashes differ)."] });
    renderAt(compareHref());
    expect(await screen.findByTestId("eval-compare-warning")).toHaveTextContent("different test suites");
  });

  it("shows an API error, and an empty compare says so", async () => {
    renderAt(compareHref());
    expect(await screen.findByTestId("eval-compare-error")).toHaveTextContent("Run not found");
    cleanup();
    fake.state.compare = compareOf({ cases: [] });
    renderAt(compareHref());
    expect(await screen.findByText("No cases to compare")).toBeInTheDocument();
  });
});

describe("View spec diff", () => {
  it("links the two versions, and the link opens the Editor's CompareSheet", async () => {
    fake.state.compare = compareOf();
    renderAt(compareHref());
    const link = await screen.findByTestId("eval-compare-spec-diff");
    expect(link).toHaveTextContent("View spec diff");
    const href = link.getAttribute("href")!;
    expect(href).toBe(routes.agentEditor(AGENT, { compare: "1..2" }));
    cleanup();
    const restore = setWindowWidth(TEST_WIDE_WIDTH);
    const media = mockMatchMedia(true);
    try {
      nextRouter.reset([href]);
      render(<FrameHarness><EditorTab agentId={AGENT} /></FrameHarness>);
      const sheet = await screen.findByTestId("compare-sheet");
      expect(sheet).toHaveTextContent(/v?1\D+v?2/);
    } finally {
      media.restore();
      restore();
    }
  });

  it("uses 'draft' for a draft run", async () => {
    fake.state.compare = compareOf({ target: side(RUN_B, null, true, 1), target_version: null });
    renderAt(compareHref());
    expect(await screen.findByTestId("eval-compare-spec-diff")).toHaveAttribute("href", routes.agentEditor(AGENT, { compare: "1..draft" }));
    expect(screen.getByTestId("eval-compare-title")).toHaveTextContent("v1 → Draft");
  });

  it("is hidden when both runs used the same version or both the draft", async () => {
    fake.state.compare = compareOf({ target: side(RUN_B, 1, false, 1), target_version: 1 });
    renderAt(compareHref());
    await screen.findAllByTestId("eval-compare-row");
    expect(screen.queryByTestId("eval-compare-spec-diff")).not.toBeInTheDocument();
    cleanup();
    fake.state.compare = compareOf({ base: side(RUN_A, null, true, 0.5), target: side(RUN_B, null, true, 1) });
    renderAt(compareHref());
    await screen.findAllByTestId("eval-compare-row");
    expect(screen.queryByTestId("eval-compare-spec-diff")).not.toBeInTheDocument();
  });
});
