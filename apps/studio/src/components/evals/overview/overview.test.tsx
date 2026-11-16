import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";
import { navSections } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { DatasetsPage } from "./DatasetsPage";
import { ExperimentsPage } from "./ExperimentsPage";
import { ScorersPage } from "./ScorersPage";

vi.mock("next/navigation", () => ({
  usePathname: () => "/datasets",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const T = "2026-11-16T10:00:00Z";
const suite = (n: number, over: Record<string, unknown> = {}) => ({
  id: `s${n}`, agent_id: "a1", workflow_id: null, target_kind: "agent", target_id: "a1", target_name: "Support bot", name: `Suite ${n}`, description: null, evaluators: [], case_count: n, archived: false, archived_at: null,
  created_at: T, updated_at: T, agent_name: "Support bot", agent_archived: false, last_run: null, ...over,
});
const lastRun = { run_id: "r1", version: 2, status: "completed", display_status: "completed", created_at: T, updated_at: T, aggregates: { cases: 4, passed: 3, failed: 1, errors: 0, pass_rate: 0.75 } };
const evalRun = (id: string, over: Record<string, unknown> = {}) => ({
  run_id: id, kind: "eval", agent_id: "a1", version: 2, status: "completed", display_status: "completed", error: null, created_at: T, updated_at: T,
  trace_id: null, suite_id: "s1", suite_hash: "h", suite_name: "Suite 1", agent_name: "Support bot", workflow_id: null, target_kind: "agent", target_id: "a1", target_name: "Support bot",
  aggregates: { cases: 4, passed: 3, failed: 1, errors: 0, pass_rate: 0.75, scores_avg: { quality: 0.8 }, duration_ms_p50: 100, cost_usd: "0.0001" }, ...over,
});
const scorer = (name: string, over: Record<string, unknown> = {}) => ({
  kind: "contains", name, suite_count: 1, suites: [{ id: "s1", name: "Suite 1", agent_id: "a1", agent_name: "Support bot", case_level_uses: 0 }],
  history: [
    { run_id: "r2", suite_id: "s1", agent_id: "a1", created_at: T, metric: "pass_rate", value: 0.8 },
    { run_id: "r1", suite_id: "s1", agent_id: "a1", created_at: T, metric: "pass_rate", value: 0.5 },
  ], ...over,
});

let responses: Record<string, (url: URL) => Response | Promise<Response>>;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  responses = {};
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://localhost");
    const handler = responses[url.pathname.replace("/api/v1", "")];
    return handler ? handler(url) : json({ detail: "missing" }, 404);
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Evaluation navigation", () => {
  it("shows the Evaluation group with Scorers, Datasets and Experiments", () => {
    const section = navSections.find((s) => s.group.id === "evaluation");
    expect(section?.group.title).toBe("Evaluation");
    expect(section?.items.map((i) => i.label)).toEqual(["Datasets", "Scorers", "Experiments"]);
    expect(section?.items.map((i) => i.href)).toEqual(["/datasets", "/scorers", "/experiments"]);
  });
});

describe("Datasets", () => {
  it("lists suites across agents and workflows with target, items and last run, linking to the dataset page", async () => {
    responses["/eval-suites"] = () =>
      json({
        items: [
          suite(4, { id: "s9", last_run: lastRun }),
          suite(2, { agent_id: "a2", agent_name: "Other bot", target_id: "a2", target_name: "Other bot" }),
          suite(3, { agent_id: null, agent_name: null, workflow_id: "wf1", target_kind: "workflow", target_id: "wf1", target_name: "Triage" }),
        ],
        next_cursor: null,
      });
    render(<DatasetsPage />);
    const rows = await screen.findAllByTestId("dataset-row");
    expect(rows).toHaveLength(3);
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(expect.arrayContaining(["Dataset", "Target", "Items", "Last run"]));
    expect(within(rows[0]).getByText("Suite 4")).toBeInTheDocument();
    expect(within(rows[0]).getByTestId("dataset-target")).toHaveTextContent("agent · Support bot");
    expect(within(rows[2]).getByTestId("dataset-target")).toHaveTextContent("workflow · Triage");
    expect(within(rows[0]).getByTestId("dataset-last-run")).toHaveTextContent("75%");
    expect(within(rows[1]).getByTestId("dataset-last-run")).toHaveTextContent("Never run");
    expect(within(rows[0]).getByRole("link", { name: /Suite 4/ })).toHaveAttribute("href", routes.dataset("s9"));
    expect(within(rows[1]).getByRole("link", { name: /Suite 2/ })).toHaveAttribute("href", "/datasets/s2");
    expect(within(rows[2]).getByRole("link", { name: /Suite 3/ })).toHaveAttribute("href", "/datasets/s3");
  });

  it("loads further pages with Load more", async () => {
    const seen: (string | null)[] = [];
    responses["/eval-suites"] = (url) => {
      seen.push(url.searchParams.get("after"));
      return url.searchParams.get("after") ? json({ items: [suite(2)], next_cursor: null }) : json({ items: [suite(1)], next_cursor: "c2" });
    };
    render(<DatasetsPage />);
    await screen.findByText("Suite 1");
    fireEvent.click(screen.getByRole("button", { name: "Load more datasets" }));
    await screen.findByText("Suite 2");
    expect(seen).toEqual([null, "c2"]);
    expect(screen.queryByRole("button", { name: "Load more datasets" })).toBeNull();
  });

  it("has an empty state, an error state with retry, and a loading state", async () => {
    responses["/eval-suites"] = () => json({ items: [], next_cursor: null });
    const { unmount } = render(<DatasetsPage />);
    expect(await screen.findByText("No datasets yet")).toBeInTheDocument();
    unmount();
    let fail = true;
    responses["/eval-suites"] = () => (fail ? json({ detail: "boom" }, 500) : json({ items: [suite(1)], next_cursor: null }));
    render(<DatasetsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(await screen.findByText("Suite 1")).toBeInTheDocument();
  });

  it("fits 375 px and has no axe violations", async () => {
    const restore = setWindowWidth(375);
    responses["/eval-suites"] = () => json({ items: [suite(1, { last_run: lastRun })], next_cursor: null });
    const { container } = render(<main><DatasetsPage /></main>);
    await screen.findByText("Suite 1");
    await expectNoAxeViolations(container);
    restore();
  }, AXE_TIMEOUT_MS);
});

describe("Scorers", () => {
  it("lists evaluators with their suites and score history, oldest first", async () => {
    responses["/evals/scorers"] = () => json({ items: [scorer("has_hi"), scorer("quality", { kind: "llm_judge", history: [{ run_id: "r1", suite_id: "s1", agent_id: "a1", created_at: T, metric: "score", value: 0.62 }] })], next_cursor: null });
    render(<ScorersPage />);
    const rows = await screen.findAllByTestId("scorer-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("has_hi")).toBeInTheDocument();
    expect(within(rows[0]).getByTestId("scorer-history")).toHaveTextContent("50% → 80%");
    expect(within(rows[0]).getByRole("link", { name: "Suite 1" })).toHaveAttribute("href", routes.agentTests("a1", { suite: "s1" }));
    expect(within(rows[1]).getByTestId("scorer-history")).toHaveTextContent("0.62");
  });

  it("links a workflow suite of a scorer to the workflow's Tests tab", async () => {
    responses["/evals/scorers"] = () =>
      json({ items: [scorer("has_hi", { suites: [{ id: "s5", name: "Wf suite", agent_id: null, agent_name: null, workflow_id: "wf1", target_kind: "workflow", target_name: "Triage", case_level_uses: 0 }] })], next_cursor: null });
    render(<ScorersPage />);
    const row = await screen.findByTestId("scorer-row");
    expect(within(row).getByRole("link", { name: "Wf suite" })).toHaveAttribute("href", routes.workflowTests("wf1", { suite: "s5" }));
  });

  it("says when a scorer has no runs yet, and has an empty state", async () => {
    responses["/evals/scorers"] = () => json({ items: [scorer("x", { history: [] })], next_cursor: null });
    const { unmount } = render(<ScorersPage />);
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
    unmount();
    responses["/evals/scorers"] = () => json({ items: [], next_cursor: null });
    render(<ScorersPage />);
    expect(await screen.findByText("No scorers yet")).toBeInTheDocument();
  });

  it("shows an error with retry", async () => {
    responses["/evals/scorers"] = () => json({ detail: "scorers down" }, 500);
    render(<ScorersPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("scorers down");
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("fits 375 px and has no axe violations", async () => {
    const restore = setWindowWidth(375);
    responses["/evals/scorers"] = () => json({ items: [scorer("has_hi")], next_cursor: null });
    const { container } = render(<main><ScorersPage /></main>);
    await screen.findByText("has_hi");
    await expectNoAxeViolations(container);
    restore();
  }, AXE_TIMEOUT_MS);
});

describe("Experiments", () => {
  it("lists eval runs across agents with status and scores, linking to the run view", async () => {
    responses["/evals/runs"] = () => json({ items: [evalRun("er2", { agent_id: "a2", agent_name: "Other bot", suite_id: "s7", suite_name: "Suite 7", display_status: "running", status: "running", aggregates: { cases: 0, passed: 0, failed: 0, errors: 0, pass_rate: null, scores_avg: {}, duration_ms_p50: null, cost_usd: null } }), evalRun("er1")], next_cursor: null });
    render(<ExperimentsPage />);
    const rows = await screen.findAllByTestId("experiment-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Suite 7")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("link")).toHaveAttribute("href", routes.agentTests("a2", { suite: "s7", run: "er2" }));
    expect(within(rows[1]).getByTestId("experiment-scores")).toHaveTextContent("quality 0.80");
    expect(within(rows[1]).getByText("75%")).toBeInTheDocument();
  });

  it("names the target of each run, agent or workflow, and opens a workflow run's suite on the workflow", async () => {
    responses["/evals/runs"] = () =>
      json({
        items: [
          evalRun("er2", { agent_id: null, agent_name: null, workflow_id: "wf1", version: null, workflow_version: 3, target_kind: "workflow", target_id: "wf1", target_name: "Triage", suite_id: "s7", suite_name: "Suite 7" }),
          evalRun("er1"),
        ],
        next_cursor: null,
      });
    render(<ExperimentsPage />);
    const rows = await screen.findAllByTestId("experiment-row");
    expect(within(rows[0]).getByTestId("experiment-target")).toHaveTextContent("workflow · Triage");
    expect(within(rows[1]).getByTestId("experiment-target")).toHaveTextContent("agent · Support bot");
    expect(within(rows[0]).getByRole("link")).toHaveAttribute("href", routes.workflowTests("wf1", { suite: "s7" }));
    expect(within(rows[0]).getByText("v3")).toBeInTheDocument();
  });

  it("has empty, error and load-more states", async () => {
    responses["/evals/runs"] = () => json({ items: [], next_cursor: null });
    const { unmount } = render(<ExperimentsPage />);
    expect(await screen.findByText("No experiments yet")).toBeInTheDocument();
    unmount();
    responses["/evals/runs"] = (url) => (url.searchParams.get("before") ? json({ detail: "page failed" }, 500) : json({ items: [evalRun("er1")], next_cursor: "c2" }));
    render(<ExperimentsPage />);
    await screen.findAllByTestId("experiment-row");
    fireEvent.click(screen.getByRole("button", { name: "Load more experiments" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("page failed"));
    expect(screen.getAllByTestId("experiment-row")).toHaveLength(1);
  });

  it("fits 375 px and has no axe violations", async () => {
    const restore = setWindowWidth(375);
    responses["/evals/runs"] = () => json({ items: [evalRun("er1")], next_cursor: null });
    const { container } = render(<main><ExperimentsPage /></main>);
    await screen.findAllByTestId("experiment-row");
    await expectNoAxeViolations(container);
    restore();
  }, AXE_TIMEOUT_MS);
});
