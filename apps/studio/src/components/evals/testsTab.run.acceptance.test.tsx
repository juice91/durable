/**
 * AC.16 acceptance: running a suite: the target, side-effect warnings ("Run anyway"), the memory
 * and retrieval notes, and idempotency through the client's run_id, against a fake of the AC.12 API.
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
import { AGENT, SUITE, callsTo, holdStart, installFakeApi, spec, suiteOf, versionOf, type FakeApi } from "./evalsFakeApi";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

let fake: FakeApi;
beforeEach(() => {
  fake = installFakeApi();
});
afterEach(() => {
  cleanup();
  fake.restore();
});

function renderSuite() {
  nextRouter.reset([routes.agentTests(AGENT, { suite: SUITE })]);
  return render(
    <TooltipProvider>
      <TestsTab agentId={AGENT} />
    </TooltipProvider>,
  );
}
async function openDialog() {
  const button = await screen.findByTestId("eval-suite-run");
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  return screen.findByTestId("eval-run-dialog");
}
const startCalls = () => callsTo(fake.state, "POST", /evals\/runs$/);
const startBody = (n = 0) => startCalls()[n].body as { run_id: string; suite_id: string; version: unknown; max_concurrency: number };
const set = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });
const withKb = { knowledge_bases: [{ id: "kb1" }] };

describe("the target", () => {
  it("defaults to the latest version and starts against it, then opens the run", async () => {
    renderSuite();
    const dialog = await openDialog();
    const target = within(dialog).getByTestId("eval-run-target");
    expect(target).toHaveValue("2");
    expect(within(target).getAllByRole("option").map((o) => o.textContent)).toEqual(["v2 (latest)", "v1", "Draft"]);
    fireEvent.click(within(dialog).getByTestId("eval-run-start"));
    await waitFor(() => expect(startCalls()).toHaveLength(1));
    expect(startBody()).toMatchObject({ suite_id: SUITE, version: 2, max_concurrency: 4 });
    expect(startBody().run_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    await waitFor(() => expect(nextRouter.url).toBe(routes.agentTests(AGENT, { suite: SUITE, run: startBody().run_id })));
  });

  it("starts against an older version with the chosen concurrency", async () => {
    renderSuite();
    const dialog = await openDialog();
    set(within(dialog).getByTestId("eval-run-target"), "1");
    set(within(dialog).getByTestId("eval-run-concurrency"), "7");
    await waitFor(() => expect(callsTo(fake.state, "GET", /evals\/warnings$/).at(-1)?.query.get("version")).toBe("1"));
    fireEvent.click(within(dialog).getByTestId("eval-run-start"));
    await waitFor(() => expect(startCalls()).toHaveLength(1));
    expect(startBody()).toMatchObject({ version: 1, max_concurrency: 7 });
  });

  it("starts against the draft", async () => {
    renderSuite();
    const dialog = await openDialog();
    set(within(dialog).getByTestId("eval-run-target"), "draft");
    expect(within(dialog).getByText(/unpublished draft/)).toBeInTheDocument();
    await waitFor(() => expect(callsTo(fake.state, "GET", /evals\/warnings$/).at(-1)?.query.get("version")).toBe("draft"));
    fireEvent.click(within(dialog).getByTestId("eval-run-start"));
    await waitFor(() => expect(startCalls()).toHaveLength(1));
    expect(startBody().version).toBe("draft");
  });

  it("an agent with no published version can only run its draft", async () => {
    fake.state.versions = [];
    fake.state.agent = { ...fake.state.agent, latest_version: null };
    renderSuite();
    const dialog = await openDialog();
    expect(within(dialog).getByTestId("eval-run-target")).toHaveValue("draft");
  });
});

describe("side-effect warnings", () => {
  const warning = { code: "tool_side_effect", tool: "send_email", kind: "builtin", message: "Tool send_email has side effects" };
  const mcp = { code: "mcp_tool_side_effect", tool: "crm.update", kind: "mcp", message: "MCP tool crm.update is not under approval" };

  it("lists each tool and turns the button into a destructive Run anyway, which starts the run", async () => {
    fake.state.warnings["2"] = [warning, mcp];
    renderSuite();
    const dialog = await openDialog();
    const list = await within(dialog).findByTestId("eval-run-warnings");
    const items = within(list).getAllByTestId("eval-run-warning");
    expect(items.map((li) => li.textContent)).toEqual(["send_email — Tool send_email has side effects", "crm.update — MCP tool crm.update is not under approval"]);
    expect(list).toHaveTextContent("Require approval");
    const start = within(dialog).getByTestId("eval-run-start");
    expect(start).toHaveTextContent("Run anyway");
    expect(start.className).toMatch(/destructive/);
    expect(startCalls()).toHaveLength(0);
    fireEvent.click(start);
    await waitFor(() => expect(startCalls()).toHaveLength(1));
  });

  it("follows the target: a version without side-effect tools runs plainly", async () => {
    fake.state.warnings["2"] = [warning];
    renderSuite();
    const dialog = await openDialog();
    await within(dialog).findByTestId("eval-run-warnings");
    set(within(dialog).getByTestId("eval-run-target"), "1");
    await waitFor(() => expect(within(dialog).queryByTestId("eval-run-warnings")).not.toBeInTheDocument());
    expect(within(dialog).getByTestId("eval-run-start")).toHaveTextContent(/^Run$/);
  });

  it("cannot start while the target's tools are still being checked", async () => {
    let answer!: () => void;
    const gate = new Promise<void>((resolve) => { answer = resolve; });
    const original = globalThis.fetch;
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      if (String(input).includes("/evals/warnings")) await gate;
      return original(input as string, init);
    }));
    renderSuite();
    const dialog = await openDialog();
    expect(within(dialog).getByTestId("eval-run-start")).toBeDisabled();
    expect(within(dialog).getByRole("status")).toHaveTextContent("Checking");
    await act(async () => answer());
    await waitFor(() => expect(within(dialog).getByTestId("eval-run-start")).toBeEnabled());
  });

  it("a failed warnings check says so and still allows a plain Run", async () => {
    fake.fail({ method: "GET", path: /evals\/warnings$/, status: 500, body: { detail: "warnings unavailable" } });
    renderSuite();
    const dialog = await openDialog();
    expect(await within(dialog).findByTestId("eval-run-warnings-error")).toHaveTextContent("warnings unavailable");
    expect(within(dialog).getByTestId("eval-run-start")).toHaveTextContent(/^Run$/);
  });
});

describe("the memory and retrieval notes", () => {
  const MEMORY = "Evaluation runs each case as a single turn without memory";
  it("an agent without memory or knowledge shows neither note", async () => {
    renderSuite();
    const dialog = await openDialog();
    expect(within(dialog).queryByTestId("eval-memory-note")).not.toBeInTheDocument();
    expect(within(dialog).queryByTestId("eval-knowledge-note")).not.toBeInTheDocument();
  });

  it("memory in the draft shows the note in the dialog and on the tab", async () => {
    fake.state.agent = { ...fake.state.agent, draft_spec: spec({ memory: { history: { last_turns: 5 } } }) };
    renderSuite();
    const dialog = await openDialog();
    expect(within(dialog).getByTestId("eval-memory-note")).toHaveTextContent(MEMORY);
    expect(screen.getAllByTestId("eval-memory-note")).toHaveLength(2);
  });

  it("memory only in the latest published version still shows it", async () => {
    fake.state.versions = [{ ...versionOf(2), spec: spec({ memory: { history: { last_turns: 2 } } }) }, versionOf(1)];
    renderSuite();
    const dialog = await openDialog();
    expect(within(dialog).getByTestId("eval-memory-note")).toBeInTheDocument();
  });

  it("the retrieval note follows the target's knowledge bases", async () => {
    fake.state.versions = [{ ...versionOf(2), spec: spec(withKb) }, versionOf(1)];
    fake.state.agent = { ...fake.state.agent, draft_spec: spec() };
    renderSuite();
    const dialog = await openDialog();
    expect(within(dialog).getByTestId("eval-knowledge-note")).toHaveTextContent("Each case runs its own retrieval");
    expect(within(dialog).getByTestId("eval-knowledge-note")).toHaveTextContent("embedding cost");
    set(within(dialog).getByTestId("eval-run-target"), "1");
    expect(within(dialog).queryByTestId("eval-knowledge-note")).not.toBeInTheDocument();
    set(within(dialog).getByTestId("eval-run-target"), "draft");
    expect(within(dialog).queryByTestId("eval-knowledge-note")).not.toBeInTheDocument();
  });

  it("a draft with knowledge bases shows the retrieval note for the draft target", async () => {
    fake.state.agent = { ...fake.state.agent, draft_spec: spec(withKb) };
    renderSuite();
    const dialog = await openDialog();
    expect(within(dialog).queryByTestId("eval-knowledge-note")).not.toBeInTheDocument();
    set(within(dialog).getByTestId("eval-run-target"), "draft");
    expect(within(dialog).getByTestId("eval-knowledge-note")).toBeInTheDocument();
  });
});

describe("idempotency through the client run_id", () => {
  it("a double click starts one run", async () => {
    const release = holdStart(fake.state);
    renderSuite();
    const dialog = await openDialog();
    const start = within(dialog).getByTestId("eval-run-start");
    fireEvent.click(start);
    fireEvent.click(start);
    await waitFor(() => expect(start).toHaveTextContent("Starting…"));
    expect(start).toBeDisabled();
    release();
    await waitFor(() => expect(nextRouter.url).toContain("run="));
    expect(startCalls()).toHaveLength(1);
    expect(fake.state.started).toHaveLength(1);
  });

  it("a double submit in one tick (Enter twice) carries the same run_id, so the server starts one run", async () => {
    const release = holdStart(fake.state);
    renderSuite();
    const dialog = await openDialog();
    const form = within(dialog).getByRole("form", { name: "Run suite" }) as HTMLFormElement;
    act(() => {
      form.requestSubmit();
      form.requestSubmit();
    });
    release();
    await waitFor(() => expect(nextRouter.url).toContain("run="));
    const ids = new Set(startCalls().map((c) => (c.body as { run_id: string }).run_id));
    expect(ids.size).toBe(1);
    expect(fake.state.started).toHaveLength(1);
  });

  it("a retry after a failed start reuses the run_id and shows the error meanwhile", async () => {
    fake.fail({ method: "POST", path: /evals\/runs$/, status: 503, body: { detail: "Eval run recorded but could not be started; retry the same request (same run_id)" }, once: true });
    renderSuite();
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByTestId("eval-run-start"));
    expect(await within(dialog).findByTestId("eval-run-error")).toHaveTextContent("retry the same request");
    expect(nextRouter.url).not.toContain("run=");
    fireEvent.click(within(dialog).getByTestId("eval-run-start"));
    await waitFor(() => expect(nextRouter.url).toContain("run="));
    expect(startCalls()).toHaveLength(2);
    expect(startBody(0).run_id).toBe(startBody(1).run_id);
    expect(nextRouter.url).toContain(startBody(0).run_id);
  });

  it("a different target is a different run, with a new run_id", async () => {
    fake.fail({ method: "POST", path: /evals\/runs$/, status: 503, body: { detail: "down" }, once: true });
    renderSuite();
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByTestId("eval-run-start"));
    await within(dialog).findByTestId("eval-run-error");
    set(within(dialog).getByTestId("eval-run-target"), "1");
    await waitFor(() => expect(within(dialog).getByTestId("eval-run-start")).toBeEnabled());
    fireEvent.click(within(dialog).getByTestId("eval-run-start"));
    await waitFor(() => expect(startCalls()).toHaveLength(2));
    expect(startBody(0).run_id).not.toBe(startBody(1).run_id);
  });

  it("reopening the dialog mints a new run_id", async () => {
    renderSuite();
    let dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("eval-run-dialog")).not.toBeInTheDocument());
    expect(startCalls()).toHaveLength(0);
    dialog = await openDialog();
    fireEvent.click(within(dialog).getByTestId("eval-run-start"));
    await waitFor(() => expect(startCalls()).toHaveLength(1));
  });
});

describe("when a run is not possible", () => {
  it("disables Run on a suite without cases and on an archived suite", async () => {
    fake.state.suites = [suiteOf({ cases: [] })];
    const empty = renderSuite();
    await screen.findByTestId("eval-suite-title");
    await waitFor(() => expect(screen.getByTestId("eval-suite-title")).toHaveTextContent("Refunds"));
    expect(screen.getByTestId("eval-suite-run")).toBeDisabled();
    empty.unmount();
    fake.state.suites = [suiteOf({ archived: true })];
    renderSuite();
    await waitFor(() => expect(screen.getByTestId("eval-suite-archived")).toBeInTheDocument());
    expect(screen.getByTestId("eval-suite-run")).toBeDisabled();
    expect(screen.getByTestId("eval-suite-import")).toBeDisabled();
  });
});
