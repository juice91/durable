/**
 * AC.16 acceptance: the Tests tab's route, tabs, deep links and URL-backed Sheets, against a fake of
 * the AC.12 API (real wire shapes, answered through a stubbed fetch).
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { nextRouter } from "@/test/nextRouterMock";
import { routes } from "@/lib/routes";
import { agentTabOf } from "@/lib/routes/agents";
import { NAV_GROUPS, allNavItems, navItems, navSections } from "@/lib/nav";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { FrameHarness, mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH } from "@/components/frame/frameTesting";
import { AgentLayout } from "@/components/agentPage/AgentLayout";
import { TestsTab } from "./TestsTab";
import { AGENT, RUN_A, RUN_B, SUITE, installFakeApi, resultOf, runOf, spec, type FakeApi } from "./evalsFakeApi";

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

describe("the Tests tab and its route", () => {
  it("agentTabOf reads /agents/x/tests, and the tab route is /agents/:id/tests", () => {
    expect(agentTabOf("/agents/x/tests")).toBe("tests");
    expect(agentTabOf("/agents/x/tests/")).toBe("tests");
    expect(routes.agentTab("a1", "tests")).toBe("/agents/a1/tests");
    expect(routes.agentTests("a 1")).toBe("/agents/a%201/tests");
  });

  it("builds deep links with suite, run and compare in that order", () => {
    expect(routes.agentTests("a1", { suite: "s1" })).toBe("/agents/a1/tests?suite=s1");
    expect(routes.agentTests("a1", { suite: "s1", run: RUN_A })).toBe(`/agents/a1/tests?suite=s1&run=${RUN_A}`);
    expect(routes.agentTests("a1", { suite: "s1", compare: `${RUN_A}..${RUN_B}` })).toContain("compare=");
  });

  it("the Evaluation group holds Datasets, Scorers and Experiments, and the Tests tab is still not a nav item", () => {
    expect(NAV_GROUPS.some((g) => g.id === "evaluation")).toBe(true);
    expect(navItems.filter((item) => item.group === "evaluation").map((item) => item.label)).toEqual(["Datasets", "Scorers", "Experiments"]);
    expect(navSections.find((section) => section.group.id === "evaluation")?.items.map((item) => item.href)).toEqual([routes.datasets(), routes.scorers(), routes.experiments()]);
    expect(allNavItems.some((item) => /\/tests/.test(item.href))).toBe(false);
  });

  it("lists the agent's suites with their case counts and the New suite action", async () => {
    renderAt(routes.agentTests(AGENT));
    const row = await screen.findByTestId("eval-suite-row");
    expect(row).toHaveTextContent("Refunds");
    expect(within(row).getByRole("link", { name: "Refunds" })).toHaveAttribute("href", routes.agentTests(AGENT, { suite: SUITE }));
    expect(screen.getAllByTestId("eval-suite-new").length).toBeGreaterThan(0);
  });

  it("shows the empty state for an agent without suites", async () => {
    fake.state.suites = [];
    renderAt(routes.agentTests(AGENT));
    expect(await screen.findByText("No test suites yet")).toBeInTheDocument();
  });
});

describe("deep links", () => {
  it("?suite= opens the suite with its evaluators and cases", async () => {
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    await waitFor(() => expect(screen.getByTestId("eval-suite-title")).toHaveTextContent("Refunds"));
    expect(await screen.findAllByTestId("eval-case-row")).toHaveLength(2);
    expect(screen.getByTestId("eval-evaluator")).toHaveAttribute("data-kind", "equals_expected");
  });

  it("?suite=&edit=<case id> opens that case's Sheet with its values", async () => {
    renderAt(`${routes.agentTests(AGENT, { suite: SUITE })}&edit=c2`);
    const sheet = await screen.findByTestId("eval-case-sheet");
    expect(within(sheet).getByTestId("eval-case-name")).toHaveValue("second");
    expect(within(sheet).getByTestId("eval-case-input")).toHaveValue("input of second");
  });

  it("?edit=suite opens the suite Sheet; ?edit=new an empty case Sheet", async () => {
    const first = renderAt(`${routes.agentTests(AGENT, { suite: SUITE })}&edit=suite`);
    expect(await screen.findByTestId("eval-suite-name")).toHaveValue("Refunds");
    first.unmount();
    renderAt(`${routes.agentTests(AGENT, { suite: SUITE })}&edit=new`);
    expect(await screen.findByTestId("eval-case-name")).toHaveValue("");
  });

  it("?run= opens the eval run and &result= its result Sheet", async () => {
    fake.state.runs = [runOf(RUN_A, 2)];
    fake.state.results[RUN_A] = [resultOf()];
    renderAt(`${routes.agentTests(AGENT, { suite: SUITE, run: RUN_A })}&result=c1`);
    expect(await screen.findByTestId("eval-result-sheet")).toHaveTextContent("Case first");
    expect(screen.getByTestId("eval-run-view")).toBeInTheDocument();
  });

  it("?compare= opens the compare view; a compare of a run with itself falls back to the suite", async () => {
    renderAt(`${routes.agentTests(AGENT, { suite: SUITE })}&compare=${RUN_A}..${RUN_A}`);
    expect(await screen.findByTestId("eval-suite-view")).toBeInTheDocument();
    expect(screen.queryByTestId("eval-compare-view")).not.toBeInTheDocument();
  });

  it("an unknown suite shows the load error with a way back", async () => {
    renderAt(routes.agentTests(AGENT, { suite: "nope" }));
    const error = await screen.findByTestId("eval-suite-load-error");
    expect(error).toHaveTextContent("Not found");
    expect(within(error).getByRole("link", { name: "All suites" })).toHaveAttribute("href", routes.agentTests(AGENT));
  });
});

describe("URL-backed Sheets", () => {
  it("opening a case pushes a history entry; Cancel removes ?edit; Back closes the Sheet", async () => {
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    fireEvent.click(within((await screen.findAllByTestId("eval-case-row"))[0]).getByRole("button", { name: "Case first" }));
    await waitFor(() => expect(nextRouter.url).toContain("edit=c1"));
    expect(nextRouter.entries).toHaveLength(2);
    fireEvent.click(within(await screen.findByTestId("eval-case-sheet")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("eval-case-sheet")).not.toBeInTheDocument());
    expect(nextRouter.url).not.toContain("edit=");
    fireEvent.click(screen.getByTestId("eval-case-add"));
    await screen.findByTestId("eval-case-sheet");
    nextRouter.back();
    await waitFor(() => expect(screen.queryByTestId("eval-case-sheet")).not.toBeInTheDocument());
  });

  it("opening a result pushes history, and Escape closes it", async () => {
    fake.state.runs = [runOf(RUN_A, 2)];
    fake.state.results[RUN_A] = [resultOf()];
    renderAt(routes.agentTests(AGENT, { suite: SUITE, run: RUN_A }));
    fireEvent.click(within((await screen.findAllByTestId("eval-result-row"))[0]).getByRole("button", { name: "Case first" }));
    await screen.findByTestId("eval-result-sheet");
    expect(nextRouter.url).toContain("result=c1");
    fireEvent.keyDown(screen.getByTestId("eval-result-sheet"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("eval-result-sheet")).not.toBeInTheDocument());
    expect(nextRouter.url).not.toContain("result=");
  });
});

describe("the agent page's tabs", () => {
  let restoreWidth: () => void;
  let media: ReturnType<typeof mockMatchMedia>;
  beforeEach(() => {
    restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
    media = mockMatchMedia(true);
  });
  afterEach(() => {
    media.restore();
    restoreWidth();
  });

  const openLayout = () => {
    nextRouter.reset([routes.agentTests(AGENT)]);
    return render(
      <FrameHarness>
        <AgentLayout agentId={AGENT}>
          <TestsTab agentId={AGENT} />
        </AgentLayout>
      </FrameHarness>,
    );
  };
  const tabNames = () => within(screen.getByRole("navigation", { name: "Agent sections" })).getAllByRole("link").map((a) => a.textContent);

  it("reads Chat · Traces · Editor · Tests for an agent without memory, Tests current", async () => {
    openLayout();
    await screen.findByTestId("tests-tab");
    await waitFor(() => expect(tabNames()).toEqual(["Chat", "Traces", "Editor", "Tests"]));
    const tests = screen.getByTestId("agent-tab-tests");
    expect(tests).toHaveAttribute("href", routes.agentTests(AGENT));
    expect(tests).toHaveAttribute("aria-current", "page");
  });

  it("puts Tests after Memory for an agent with memory, and shows the memory note", async () => {
    fake.state.agent = { ...fake.state.agent, draft_spec: spec({ memory: { history: { last_turns: 5 } } }) };
    openLayout();
    await waitFor(() => expect(tabNames()).toEqual(["Chat", "Traces", "Editor", "Memory", "Tests"]));
    expect(await screen.findByTestId("eval-memory-note")).toHaveTextContent("Evaluation runs each case as a single turn without memory");
  });

  it("the Tests tab link is reachable by keyboard in tab order", async () => {
    openLayout();
    await screen.findByTestId("tests-tab");
    const tests = await screen.findByTestId("agent-tab-tests");
    tests.focus();
    expect(tests).toHaveFocus();
    expect(tests).not.toHaveAttribute("tabindex", "-1");
  });
});
