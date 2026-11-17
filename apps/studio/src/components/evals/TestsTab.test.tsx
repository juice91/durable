import { beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { nextRouter } from "@/test/nextRouterMock";
import { api, type Agent, type AgentVersion } from "@/lib/api";
import { routes } from "@/lib/routes";
import { subscribeRun, getRun } from "@/lib/runs";
import {
  compareEvalRuns,
  getEvalRun,
  getSuite,
  getWarnings,
  listAllResults,
  listEvalRuns,
  listSuites,
  startEvalRun,
  updateCase,
  type EvalCaseResult,
  type EvalCompare,
  type EvalRun,
  type EvalSuiteWithCases,
} from "@/lib/evals";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { TestsTab } from "./TestsTab";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...original,
    api: { ...original.api, agents: { ...original.api.agents, get: vi.fn() }, versions: { ...original.api.versions, list: vi.fn() } },
  };
});
vi.mock("@/lib/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/runs")>()),
  subscribeRun: vi.fn(() => () => undefined),
  getRun: vi.fn(),
}));
vi.mock("@/components/agents/specSections", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/agents/specSections")>()),
  fetchModels: vi.fn(async () => [
    { id: "test:judge", provider: "test", name: "judge", wildcard: false, source: "scripted", description: null },
    { id: "openai:*", provider: "openai", name: "*", wildcard: true, source: "allowlist", description: null },
  ]),
}));
vi.mock("@/lib/evals", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/evals")>()),
  listSuites: vi.fn(),
  getSuite: vi.fn(),
  listEvalRuns: vi.fn(),
  getWarnings: vi.fn(),
  startEvalRun: vi.fn(),
  getEvalRun: vi.fn(),
  listAllResults: vi.fn(),
  compareEvalRuns: vi.fn(),
  updateCase: vi.fn(),
}));

const AGENT = "a1";
const SUITE = "s1";
const RUN_A = "11111111-2222-3333-4444-555555555555";
const RUN_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

const spec = (extra: Record<string, unknown> = {}) => ({ spec_version: 1, name: "Bot", instructions: "Be nice", model: { provider: "test", name: "echo" }, tools: [], ...extra }) as never;
const agent = (extra: Record<string, unknown> = {}): Agent => ({ id: AGENT, name: "Bot", draft_spec: spec(extra), latest_version: 2 });
const versions: AgentVersion[] = [
  { agent_id: AGENT, version: 2, spec: spec(), spec_hash: "h2", created_at: "", created_by: "" },
  { agent_id: AGENT, version: 1, spec: spec(), spec_hash: "h1", created_at: "", created_by: "" },
];

const suite: EvalSuiteWithCases = {
  id: SUITE,
  agent_id: AGENT,
  name: "Refunds",
  description: "Refund questions",
  evaluators: [{ kind: "equals_expected" }, { kind: "llm_judge", rubric: "MUST_CONTAIN:ok", model: { provider: "test", name: "judge" } }],
  case_count: 1,
  archived: false,
  archived_at: null,
  created_at: null,
  updated_at: null,
  cases: [
    { id: "c1", suite_id: SUITE, name: "first", input: "hello", expected_output: "hello", metadata: {}, evaluators: [], position: 0, created_at: null, updated_at: null },
  ],
};

const run = (id: string, version: number | null, status = "completed", passRate = 0.5): EvalRun => ({
  run_id: id,
  agent_id: AGENT,
  version,
  status,
  display_status: status,
  error: null,
  created_at: "2026-11-15T10:00:00+00:00",
  updated_at: "2026-11-15T10:00:00+00:00",
  trace_id: null,
  suite_id: SUITE,
  suite_hash: "h",
  aggregates: { cases: 2, passed: 1, failed: 1, errors: 0, pass_rate: passRate, scores_avg: { llm_judge: 0.5 }, duration_ms_p50: 120, cost_usd: "0.0020" },
});

const result = (patch: Partial<EvalCaseResult>): EvalCaseResult => ({
  case_id: "c1",
  case_name: "first",
  position: 0,
  child_run_id: "child-1",
  status: "passed",
  output: "hello",
  assertions: { equals_expected: { value: true }, llm_judge: { value: true, reason: "Found MUST_CONTAIN:ok" } },
  scores: { llm_judge: { value: 1, reason: "Found MUST_CONTAIN:ok" } },
  labels: {},
  evaluator_failures: [],
  duration_ms: 100,
  cost_usd: "0.0010",
  error_code: null,
  created_at: null,
  ...patch,
});

function renderAt(href: string) {
  nextRouter.reset([href]);
  // The agent page (AgentLayout) provides the tooltip provider.
  return render(
    <TooltipProvider>
      <TestsTab agentId={AGENT} />
    </TooltipProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.agents.get).mockReset().mockResolvedValue(agent());
  vi.mocked(api.versions.list).mockReset().mockResolvedValue(versions);
  vi.mocked(listSuites).mockReset().mockResolvedValue([]);
  vi.mocked(getSuite).mockReset().mockResolvedValue(suite);
  vi.mocked(listEvalRuns).mockReset().mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(getWarnings).mockReset().mockResolvedValue({ version: 2, draft: false, warnings: [] });
  vi.mocked(startEvalRun).mockReset();
  vi.mocked(getEvalRun).mockReset();
  vi.mocked(listAllResults).mockReset();
  vi.mocked(compareEvalRuns).mockReset().mockReturnValue(new Promise(() => undefined));
  vi.mocked(updateCase).mockReset();
  vi.mocked(subscribeRun).mockClear();
  vi.mocked(getRun).mockReset().mockResolvedValue({ input: JSON.stringify({ cases: [{}, {}] }) } as never);
});

describe("Tests tab: suites", () => {
  it("shows the empty state, and no memory note for an agent without memory", async () => {
    renderAt(routes.agentTests(AGENT));
    expect(await screen.findByText("No test suites yet")).toBeInTheDocument();
    expect(screen.queryByTestId("eval-memory-note")).not.toBeInTheDocument();
  });

  it("shows the memory note on an agent with memory", async () => {
    vi.mocked(api.agents.get).mockResolvedValue(agent({ memory: { history: { last_turns: 5 } } }));
    renderAt(routes.agentTests(AGENT));
    expect(await screen.findByTestId("eval-memory-note")).toHaveTextContent("Evaluation runs each case as a single turn without memory");
  });

  it("lists suites with their last run, each row a link to the suite", async () => {
    vi.mocked(listSuites).mockResolvedValue([suite]);
    vi.mocked(listEvalRuns).mockResolvedValue({ items: [run(RUN_A, 2)], next_cursor: null });
    renderAt(routes.agentTests(AGENT));
    const row = await screen.findByTestId("eval-suite-row");
    expect(within(row).getByRole("link", { name: "Refunds" })).toHaveAttribute("href", routes.agentTests(AGENT, { suite: SUITE }));
    await waitFor(() => expect(row).toHaveTextContent("v2 · 50%"));
  });
});

describe("Tests tab: a suite", () => {
  it("shows evaluators and cases; a case row opens the case Sheet, which saves a JSON expected output", async () => {
    vi.mocked(updateCase).mockImplementation(async (_s, _c, body) => ({ ...suite.cases[0], ...body }) as never);
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    await waitFor(() => expect(screen.getByTestId("eval-suite-title")).toHaveTextContent("Refunds"));
    expect(screen.getAllByTestId("eval-evaluator")).toHaveLength(2);
    fireEvent.click(within(await screen.findByTestId("eval-case-row")).getByRole("button", { name: "Case first" }));
    await waitFor(() => expect(nextRouter.url).toContain("edit=c1"));
    const sheet = await screen.findByTestId("eval-case-sheet");
    expect(within(sheet).getByTestId("eval-case-expected")).toHaveAttribute("data-mode", "text");
    fireEvent.click(within(sheet).getByRole("radio", { name: "JSON" }));
    fireEvent.change(within(sheet).getByTestId("eval-case-expected"), { target: { value: '{"answer": "hello"}' } });
    fireEvent.click(within(sheet).getByTestId("eval-case-save"));
    await waitFor(() => expect(updateCase).toHaveBeenCalled());
    expect(vi.mocked(updateCase).mock.calls[0][2]).toMatchObject({ name: "first", input: "hello", expected_output: { answer: "hello" }, metadata: {}, evaluators: [] });
  });

  it("runs against a version: side-effect warnings make it 'Run anyway', and the run opens", async () => {
    vi.mocked(getWarnings).mockImplementation(async (_a, target) => ({
      version: typeof target === "number" ? target : null,
      draft: target === "draft",
      warnings: target === "draft" ? [] : [{ code: "tool_side_effect", tool: "send_email", kind: "builtin", message: "Tool send_email has side effects" }],
    }));
    vi.mocked(startEvalRun).mockImplementation(async (_a, body) => ({ run_id: body.run_id, warnings: [] }) as never);
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    fireEvent.click(await screen.findByTestId("eval-suite-run"));
    const dialog = await screen.findByTestId("eval-run-dialog");
    expect(within(dialog).getByTestId("eval-run-target")).toHaveValue("2");
    expect(await within(dialog).findByTestId("eval-run-warning")).toHaveTextContent("send_email");
    expect(within(dialog).getByTestId("eval-run-start")).toHaveTextContent("Run anyway");
    // The draft has no side-effect tools: a plain Run.
    fireEvent.change(within(dialog).getByTestId("eval-run-target"), { target: { value: "draft" } });
    await waitFor(() => expect(within(dialog).getByTestId("eval-run-start")).toHaveTextContent(/^Run$/));
    fireEvent.click(within(dialog).getByTestId("eval-run-start"));
    await waitFor(() => expect(startEvalRun).toHaveBeenCalled());
    const body = vi.mocked(startEvalRun).mock.calls[0][1];
    expect(body).toMatchObject({ suite_id: SUITE, version: "draft", max_concurrency: 4 });
    await waitFor(() => expect(nextRouter.url).toBe(routes.agentTests(AGENT, { suite: SUITE, run: body.run_id })));
  });

  it("shows the knowledge note only when the target's spec has knowledge bases", async () => {
    const kb = { knowledge_bases: [{ id: "kb1" }] };
    vi.mocked(api.agents.get).mockResolvedValue(agent(kb));
    vi.mocked(api.versions.list).mockResolvedValue([{ ...versions[0], spec: spec() }, { ...versions[1], spec: spec(kb) }]);
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    fireEvent.click(await screen.findByTestId("eval-suite-run"));
    const dialog = await screen.findByTestId("eval-run-dialog");
    // v2 (latest) has no knowledge.
    expect(within(dialog).queryByTestId("eval-knowledge-note")).not.toBeInTheDocument();
    fireEvent.change(within(dialog).getByTestId("eval-run-target"), { target: { value: "1" } });
    expect(within(dialog).getByTestId("eval-knowledge-note")).toHaveTextContent("Each case runs its own retrieval, and its embedding cost counts in that case's cost");
    fireEvent.change(within(dialog).getByTestId("eval-run-target"), { target: { value: "draft" } });
    expect(within(dialog).getByTestId("eval-knowledge-note")).toBeInTheDocument();
    expect(within(dialog).queryByTestId("eval-memory-note")).not.toBeInTheDocument();
    // Let the target's warnings check settle.
    await waitFor(() => expect(within(dialog).getByTestId("eval-run-start")).not.toBeDisabled());
  });

  it("shows the run history and opens a compare of two runs", async () => {
    vi.mocked(listEvalRuns).mockResolvedValue({ items: [run(RUN_B, 2), run(RUN_A, 1)], next_cursor: null });
    renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    expect(await screen.findAllByTestId("eval-history-row")).toHaveLength(2);
    fireEvent.change(screen.getByTestId("eval-compare-base"), { target: { value: RUN_A } });
    fireEvent.change(screen.getByTestId("eval-compare-target"), { target: { value: RUN_B } });
    fireEvent.click(screen.getByTestId("eval-compare-open"));
    await waitFor(() => expect(nextRouter.url).toBe(routes.agentTests(AGENT, { suite: SUITE, compare: `${RUN_A}..${RUN_B}` })));
  });
});

describe("Tests tab: an eval run", () => {
  it("shows live progress while running and refreshes on eval_case_result", async () => {
    vi.mocked(getEvalRun).mockResolvedValue(run(RUN_A, 2, "running"));
    vi.mocked(listAllResults).mockResolvedValueOnce([]).mockResolvedValue([result({})]);
    renderAt(routes.agentTests(AGENT, { suite: SUITE, run: RUN_A }));
    expect(await screen.findByTestId("eval-progress-count")).toHaveTextContent("0 of 2");
    await waitFor(() => expect(subscribeRun).toHaveBeenCalled());
    const handlers = vi.mocked(subscribeRun).mock.calls[0][1];
    handlers.onEvent?.({ seq: 3, type: "eval_case_result", data: {}, ts: "" });
    await waitFor(() => expect(screen.getByTestId("eval-progress-count")).toHaveTextContent("1 of 2"));
  });

  it("lists results with failure and judge reasons; a row opens the per-evaluator Sheet", async () => {
    vi.mocked(getEvalRun).mockResolvedValue(run(RUN_A, 2));
    vi.mocked(listAllResults).mockResolvedValue([
      result({}),
      result({
        case_id: "c2",
        case_name: "second",
        position: 1,
        status: "failed",
        assertions: { equals_expected: { value: true }, llm_judge: { value: false, reason: "No MUST_CONTAIN:ok marker" } },
        scores: { llm_judge: { value: 0, reason: "No MUST_CONTAIN:ok marker" } },
      }),
    ]);
    renderAt(routes.agentTests(AGENT, { suite: SUITE, run: RUN_A }));
    expect(await screen.findByTestId("eval-kpi-pass-rate")).toHaveTextContent("50%");
    const rows = await screen.findAllByTestId("eval-result-row");
    expect(rows[1]).toHaveTextContent("llm_judge: No MUST_CONTAIN:ok marker");
    fireEvent.click(within(rows[1]).getByRole("button", { name: "Case second" }));
    const sheet = await screen.findByTestId("eval-result-sheet");
    const judge = within(within(sheet).getByTestId("eval-result-assertions")).getAllByTestId("eval-outcome").find((el) => el.getAttribute("data-name") === "llm_judge")!;
    expect(judge).toHaveTextContent("Failed");
    expect(judge).toHaveTextContent("No MUST_CONTAIN:ok marker");
    expect(within(sheet).getByTestId("eval-result-trace")).toHaveAttribute("href", expect.stringContaining("child-1"));
  });
});

describe("Tests tab: run history stays current", () => {
  it("re-reads the runs when the watched run finishes, and on coming back to the suite", async () => {
    vi.mocked(listEvalRuns).mockResolvedValue({ items: [run(RUN_A, 2, "queued")], next_cursor: null });
    vi.mocked(getEvalRun).mockResolvedValue(run(RUN_A, 2, "running"));
    vi.mocked(listAllResults).mockResolvedValue([]);
    renderAt(routes.agentTests(AGENT, { suite: SUITE, run: RUN_A }));
    await waitFor(() => expect(subscribeRun).toHaveBeenCalled());
    // The runs are read for this suite only.
    expect(vi.mocked(listEvalRuns).mock.calls[0][1]).toMatchObject({ suiteId: SUITE });
    const before = vi.mocked(listEvalRuns).mock.calls.length;
    vi.mocked(getEvalRun).mockResolvedValue(run(RUN_A, 2, "completed", 1));
    vi.mocked(listEvalRuns).mockResolvedValue({ items: [run(RUN_A, 2, "completed", 1)], next_cursor: null });
    vi.mocked(subscribeRun).mock.calls[0][1].onEvent?.({ seq: 9, type: "run_completed", data: {}, ts: "" });
    await waitFor(() => expect(vi.mocked(listEvalRuns).mock.calls.length).toBeGreaterThan(before));
    const afterFinish = vi.mocked(listEvalRuns).mock.calls.length;
    // Back to the suite (what the "Suite" link does).
    expect(await screen.findByTestId("eval-run-back")).toHaveAttribute("href", routes.agentTests(AGENT, { suite: SUITE }));
    await act(async () => {
      nextRouter.push(routes.agentTests(AGENT, { suite: SUITE }));
      await nextRouter.settle();
    });
    await waitFor(() => expect(vi.mocked(listEvalRuns).mock.calls.length).toBeGreaterThan(afterFinish));
    const row = await screen.findByTestId("eval-history-row");
    await waitFor(() => expect(row).toHaveTextContent("100%"));
  });
});

describe("Tests tab: compare", () => {
  const compare: EvalCompare = {
    base: { run_id: RUN_A, agent_id: AGENT, agent_version: 1, draft: false, suite_id: SUITE, suite_hash: "h1", status: "completed", aggregates: run(RUN_A, 1, "completed", 0.5).aggregates! },
    target: { run_id: RUN_B, agent_id: AGENT, agent_version: 2, draft: false, suite_id: SUITE, suite_hash: "h2", status: "completed", aggregates: run(RUN_B, 2, "completed", 1).aggregates! },
    same_suite: false,
    warnings: ["The runs used different test suites (their suite hashes differ)."],
    summary: { fixed: 1, regressed: 1, unchanged: 0, added: 1, removed: 0, pass_rate_delta: 0.5, scores_avg_delta: { llm_judge: 0.5 }, cost_usd_delta: "-0.0010", duration_ms_p50_delta: -20 },
    cases: [
      { name: "first", base_case_id: "c1", target_case_id: "c1", base_status: "failed", target_status: "passed", change: "fixed", assertions: { equals_expected: { base: false, target: true } }, score_deltas: {}, duration_ms_delta: -10, cost_usd_delta: "0" },
      { name: "second", base_case_id: "c2", target_case_id: "c2", base_status: "passed", target_status: "failed", change: "regressed", assertions: {}, score_deltas: {}, duration_ms_delta: 5, cost_usd_delta: "0.0001" },
      { name: "third", base_case_id: null, target_case_id: "c3", base_status: null, target_status: "passed", change: "added", assertions: {}, score_deltas: {}, duration_ms_delta: null, cost_usd_delta: null },
    ],
    base_version: 1,
    target_version: 2,
  };

  it("classifies cases, shows the suite warning and deltas, and links the spec diff", async () => {
    vi.mocked(compareEvalRuns).mockResolvedValue(compare);
    renderAt(routes.agentTests(AGENT, { suite: SUITE, compare: `${RUN_A}..${RUN_B}` }));
    await waitFor(() => expect(screen.getByTestId("eval-compare-title")).toHaveTextContent("v1 → v2"));
    expect(compareEvalRuns).toHaveBeenCalledWith(AGENT, RUN_A, RUN_B, expect.anything());
    const changes = (await screen.findAllByTestId("eval-compare-change")).map((el) => el.getAttribute("data-change"));
    expect(changes).toEqual(["fixed", "regressed", "added"]);
    expect(screen.getByTestId("eval-compare-warning")).toHaveTextContent("different test suites");
    expect(screen.getByTestId("eval-compare-pass-rate-delta")).toHaveTextContent("+50 pts");
    expect(screen.getByTestId("eval-compare-cost-delta")).toHaveAttribute("data-tone", "better");
    expect(screen.getByTestId("eval-compare-spec-diff")).toHaveAttribute("href", routes.agentEditor(AGENT, { compare: "1..2" }));
  });
});

describe("Tests tab: accessibility", () => {
  it("has no axe violations on a suite and on a result Sheet", async () => {
    vi.mocked(listEvalRuns).mockResolvedValue({ items: [run(RUN_B, 2), run(RUN_A, 1)], next_cursor: null });
    const { container, unmount } = renderAt(routes.agentTests(AGENT, { suite: SUITE }));
    await screen.findAllByTestId("eval-history-row");
    await screen.findByTestId("eval-case-row");
    await expectNoAxeViolations(container);
    unmount();
    vi.mocked(getEvalRun).mockResolvedValue(run(RUN_A, 2));
    vi.mocked(listAllResults).mockResolvedValue([result({})]);
    renderAt(`${routes.agentTests(AGENT, { suite: SUITE, run: RUN_A })}&result=c1`);
    await expectNoAxeViolations(await screen.findByTestId("eval-result-sheet"));
  }, AXE_TIMEOUT_MS);
});
