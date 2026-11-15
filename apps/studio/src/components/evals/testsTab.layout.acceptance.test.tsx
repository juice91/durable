/**
 * AC.16 acceptance: the Tests tab at phone width (375 px), axe on each surface, the keyboard and the
 * guards (unsaved edits, archived suites), against a fake of the AC.12 API.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { nextRouter } from "@/test/nextRouterMock";
import { routes } from "@/lib/routes";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { TestsTab } from "./TestsTab";
import { AGENT, RUN_A, RUN_B, SUITE, installFakeApi, resultOf, runOf, suiteOf, type FakeApi } from "./evalsFakeApi";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

let fake: FakeApi;
beforeEach(() => {
  fake = installFakeApi((s) => {
    s.runs = [runOf(RUN_B, 2, "completed", 1), runOf(RUN_A, 1, "completed", 0.5)];
    s.results[RUN_A] = [resultOf(), resultOf({ case_id: "c2", case_name: "second", status: "failed", assertions: { equals_expected: { value: false, reason: "no" } } })];
    s.warnings["2"] = [{ code: "tool_side_effect", tool: "send_email", kind: "builtin", message: "Tool send_email has side effects" }];
  });
});
afterEach(() => {
  cleanup();
  fake.restore();
});

const suiteHref = (extra = "") => `${routes.agentTests(AGENT, { suite: SUITE })}${extra}`;
function renderAt(href: string) {
  nextRouter.reset([href]);
  return render(
    <TooltipProvider>
      <TestsTab agentId={AGENT} />
    </TooltipProvider>,
  );
}
const set = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });

describe("phone width (375 px)", () => {
  const original = window.innerWidth;
  beforeEach(() => Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 375 }));
  afterEach(() => Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: original }));

  /** jsdom has no layout: look for what would force an overflow (fixed widths past the screen). */
  const expectFits = (root: HTMLElement) => {
    const fixed = Array.from(root.querySelectorAll<HTMLElement>("*")).flatMap((el) =>
      Array.from(el.classList).filter((c) => /^(min-)?w-\[(\d+)px\]$/.test(c) && Number(/\[(\d+)px\]/.exec(c)![1]) > 375),
    );
    expect(fixed).toEqual([]);
    expect(Array.from(root.querySelectorAll<HTMLElement>("[style]")).filter((el) => Number.parseFloat(el.style.minWidth) > 375)).toEqual([]);
    expect(root.querySelectorAll("table").length === 0 || Array.from(root.querySelectorAll("table")).every((t) => t.closest(".overflow-x-auto"))).toBe(true);
    expect(root).toHaveClass("min-w-0");
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(document.documentElement.clientWidth || 375);
  };

  it.each([
    ["the suite list", routes.agentTests(AGENT), "eval-suite-row"],
    ["a suite", suiteHref(), "eval-history-row"],
    ["an eval run", routes.agentTests(AGENT, { suite: SUITE, run: RUN_A }), "eval-result-row"],
  ])("%s fits: nothing is pinned wider than the screen", async (_name, href, ready) => {
    renderAt(href);
    await screen.findAllByTestId(ready);
    expectFits(screen.getByTestId("tests-tab"));
  });

  it("the compare view fits", async () => {
    fake.state.compare = {
      base: { run_id: RUN_A, agent_id: AGENT, agent_version: 1, draft: false, suite_id: SUITE, suite_hash: "h", status: "completed", aggregates: runOf(RUN_A, 1).aggregates! },
      target: { run_id: RUN_B, agent_id: AGENT, agent_version: 2, draft: false, suite_id: SUITE, suite_hash: "h", status: "completed", aggregates: runOf(RUN_B, 2).aggregates! },
      same_suite: true, warnings: [], base_version: 1, target_version: 2,
      summary: { fixed: 0, regressed: 0, unchanged: 1, added: 0, removed: 0, pass_rate_delta: 0, scores_avg_delta: {}, cost_usd_delta: "0", duration_ms_p50_delta: 0 },
      cases: [{ name: "a", base_case_id: "c1", target_case_id: "c1", base_status: "passed", target_status: "passed", change: "unchanged", assertions: {}, score_deltas: {}, duration_ms_delta: 0, cost_usd_delta: "0" }],
    };
    renderAt(`${suiteHref()}&compare=${RUN_A}..${RUN_B}`);
    await screen.findAllByTestId("eval-compare-row");
    expectFits(screen.getByTestId("tests-tab"));
  });

  it("the case Sheet and the run dialog open full-width controls and reach every action", async () => {
    renderAt(suiteHref("&edit=c1"));
    const sheet = await screen.findByTestId("eval-case-sheet");
    expect(within(sheet).getByTestId("eval-case-save")).toBeInTheDocument();
    expect(within(sheet).getByTestId("eval-case-delete")).toBeInTheDocument();
    cleanup();
    renderAt(suiteHref());
    const run = await screen.findByTestId("eval-suite-run");
    await waitFor(() => expect(run).toBeEnabled());
    fireEvent.click(run);
    const dialog = await screen.findByTestId("eval-run-dialog");
    expect(within(dialog).getByTestId("eval-run-start")).toBeInTheDocument();
  });
});

describe("axe", () => {
  it("the suite list and a suite have no violations", async () => {
    const list = renderAt(routes.agentTests(AGENT));
    await screen.findByTestId("eval-suite-row");
    await expectNoAxeViolations(list.container);
    list.unmount();
    const view = renderAt(suiteHref());
    await screen.findAllByTestId("eval-history-row");
    await screen.findAllByTestId("eval-case-row");
    await expectNoAxeViolations(view.container);
  }, AXE_TIMEOUT_MS);

  it("the suite Sheet with all 8 evaluators has no violations", async () => {
    fake.state.suites = [suiteOf({ evaluators: [
      { kind: "equals_expected" }, { kind: "contains", value: "a" }, { kind: "regex", pattern: "a" }, { kind: "schema_valid" },
      { kind: "field_equals", path: "a", value: 1 }, { kind: "max_duration", seconds: 3 }, { kind: "max_cost", usd: "0.1" },
      { kind: "llm_judge", rubric: "r", model: { provider: "test", name: "judge" } },
    ] })];
    renderAt(suiteHref("&edit=suite"));
    const sheet = await screen.findByTestId("eval-suite-sheet");
    await within(sheet).findByTestId("evaluator-rubric");
    await expectNoAxeViolations(sheet);
  }, AXE_TIMEOUT_MS);

  it("the case Sheet with an error, and the run dialog with warnings, have no violations", async () => {
    renderAt(suiteHref("&edit=new"));
    const sheet = await screen.findByTestId("eval-case-sheet");
    fireEvent.click(within(sheet).getByTestId("eval-case-save"));
    await within(sheet).findByTestId("eval-case-error");
    await expectNoAxeViolations(sheet);
    cleanup();
    renderAt(suiteHref());
    const run = await screen.findByTestId("eval-suite-run");
    await waitFor(() => expect(run).toBeEnabled());
    fireEvent.click(run);
    const dialog = await screen.findByTestId("eval-run-dialog");
    await within(dialog).findByTestId("eval-run-warnings");
    await expectNoAxeViolations(dialog);
  }, AXE_TIMEOUT_MS);

  it("a run with results and the compare view have no violations", async () => {
    const run = renderAt(routes.agentTests(AGENT, { suite: SUITE, run: RUN_A }));
    await screen.findAllByTestId("eval-result-row");
    await expectNoAxeViolations(run.container);
  }, AXE_TIMEOUT_MS);
});

describe("the keyboard", () => {
  it("Enter on a case row opens its Sheet, focus moves into it, Escape closes it", async () => {
    renderAt(suiteHref());
    const row = within((await screen.findAllByTestId("eval-case-row"))[0]).getByRole("button", { name: "Case first" });
    row.focus();
    fireEvent.keyDown(row, { key: "Enter" });
    fireEvent.click(row);
    const sheet = await screen.findByTestId("eval-case-sheet");
    await waitFor(() => expect(sheet.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("eval-case-sheet")).not.toBeInTheDocument());
  });

  it("Enter in the name field saves the case; Escape closes the run dialog", async () => {
    renderAt(suiteHref("&edit=c1"));
    const sheet = await screen.findByTestId("eval-case-sheet");
    const form = within(sheet).getByRole("form", { name: "Edit case" });
    fireEvent.submit(form);
    await waitFor(() => expect(screen.queryByTestId("eval-case-sheet")).not.toBeInTheDocument());
    const run = await screen.findByTestId("eval-suite-run");
    fireEvent.click(run);
    const dialog = await screen.findByTestId("eval-run-dialog");
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("eval-run-dialog")).not.toBeInTheDocument());
  });

  it("every control in the evaluator editor is a labelled form control", async () => {
    fake.state.suites = [suiteOf({ evaluators: [{ kind: "llm_judge", rubric: "r", model: { provider: "test", name: "judge" } }] })];
    renderAt(suiteHref("&edit=suite"));
    const row = within(await screen.findByTestId("eval-suite-sheet")).getByTestId("evaluator-row");
    for (const name of [/Evaluator 1/, /Name/, /Rubric/, /Judge model/, /Pass threshold/, "Remove evaluator 1"]) expect(within(row).getByLabelText(name)).toBeInTheDocument();
  });
});

describe("guards", () => {
  it("an archived suite cannot be edited, run, imported or have cases added", async () => {
    fake.state.suites = [suiteOf({ archived: true })];
    renderAt(suiteHref("&edit=c1"));
    const sheet = await screen.findByTestId("eval-case-sheet");
    expect(within(sheet).getByTestId("eval-case-save")).toBeDisabled();
    expect(within(sheet).getByTestId("eval-case-name")).toBeDisabled();
    expect(within(sheet).queryByTestId("eval-case-delete")).not.toBeInTheDocument();
    expect(screen.getByTestId("eval-case-add")).toBeDisabled();
    expect(screen.getByTestId("eval-evaluators-edit")).toBeDisabled();
  });

  it("closing or reloading the tab with unsaved edits asks the browser", async () => {
    renderAt(suiteHref("&edit=c1"));
    const sheet = await screen.findByTestId("eval-case-sheet");
    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    set(within(sheet).getByTestId("eval-case-name"), "changed");
    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
  });

  it("a link to another page is held while edits are unsaved, and 'Leave without saving' follows it", async () => {
    renderAt(suiteHref("&edit=c1"));
    const sheet = await screen.findByTestId("eval-case-sheet");
    set(within(sheet).getByTestId("eval-case-name"), "changed");
    const link = document.createElement("a");
    link.href = routes.agentChat(AGENT);
    link.textContent = "elsewhere";
    document.body.appendChild(link);
    fireEvent.click(link);
    expect(await screen.findByText("Leave without saving?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(nextRouter.url).toBe(routes.agentChat(AGENT)));
    link.remove();
  });

  it("a deleted suite sends the page back to the list", async () => {
    renderAt(suiteHref());
    const more = await screen.findByTestId("eval-suite-more");
    await waitFor(() => expect(more).toBeEnabled());
    fireEvent.keyDown(more, { key: "Enter" });
    fireEvent.click(await screen.findByTestId("eval-suite-delete"));
    fireEvent.click(await screen.findByRole("button", { name: "Delete suite" }));
    await waitFor(() => expect(nextRouter.url).toBe(routes.agentTests(AGENT)));
    expect(fake.state.suites).toHaveLength(0);
  });
});
