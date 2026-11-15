/** AC.15 acceptance: the inbox list over fetch (query strings), filters and the URL state. */
import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextRouter } from "@/test/nextRouterMock";
import { resetPendingApprovalsForTests } from "@/lib/approvals";
import { routes } from "@/lib/routes";
import ApprovalsPage from "./page";
import { AGENT, AGENT2, RUN, WF_RUN, decidedTool, jsonResponse, stepItem, toolItem } from "./approvalsAcceptanceKit";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

const fetchMock = vi.fn();
const listCalls = () => fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => /\/approvals\?/.test(u) && !u.includes("limit=100"));
const lastList = () => new URL(listCalls().at(-1)!, "http://x").searchParams;

beforeEach(() => {
  nextRouter.reset(["/approvals"]);
  resetPendingApprovalsForTests();
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/agents")) return jsonResponse([{ id: AGENT, name: "Support bot" }, { id: AGENT2, name: "Billing bot" }]);
    if (url.includes("status=decided")) return jsonResponse({ items: [decidedTool], next_cursor: null });
    return jsonResponse({ items: [toolItem, stepItem], next_cursor: null });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPendingApprovalsForTests();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("inbox list", () => {
  it("renders pending items of both kinds in a DataList with the default query", async () => {
    render(<ApprovalsPage />);
    const list = await screen.findByTestId("approvals-list");
    await within(list).findByTestId(`approval-row-${RUN}:call_1`);
    expect(within(list).getByTestId(`approval-row-${WF_RUN}:review`)).toBeInTheDocument();
    expect(within(list).getByTestId(`approval-row-status-${RUN}:call_1`)).toHaveTextContent(/pending/i);
    const q = lastList();
    expect(q.get("status")).toBe("pending");
    expect(q.has("kind")).toBe(false);
    expect(q.has("agent_id")).toBe(false);
  });

  it("the Decided radio shows decided items with their decision and writes ?status=decided", async () => {
    render(<ApprovalsPage />);
    await screen.findByTestId(`approval-row-${RUN}:call_1`);
    fireEvent.click(screen.getByRole("radio", { name: "Decided" }));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe(routes.approvals({ status: "decided" }));
    expect(await screen.findByTestId(`approval-row-${RUN}:call_d`)).toHaveTextContent("refund");
    expect(screen.getByTestId(`approval-row-status-${RUN}:call_d`)).toHaveTextContent("Approved");
    expect(screen.queryByTestId(`approval-row-${RUN}:call_1`)).toBeNull();
    expect(lastList().get("status")).toBe("decided");
  });

  it("filters come from the URL on load (deep link) and are sent to the API", async () => {
    nextRouter.reset([routes.approvals({ status: "decided", kind: "tool", agent: AGENT })]);
    render(<ApprovalsPage />);
    await screen.findByTestId(`approval-row-${RUN}:call_d`);
    const q = lastList();
    expect([q.get("status"), q.get("kind"), q.get("agent_id")]).toEqual(["decided", "tool", AGENT]);
    expect(screen.getByRole("radio", { name: "Decided" })).toBeChecked();
    expect(screen.getByTestId("approvals-kind-filter")).toHaveValue("tool");
    await waitFor(() => expect(screen.getByTestId("approvals-agent-filter")).toHaveValue(AGENT));
  });

  it("Clear filters resets kind and agent but keeps the status", async () => {
    nextRouter.reset([routes.approvals({ status: "decided", kind: "step", agent: AGENT })]);
    render(<ApprovalsPage />);
    fireEvent.click(await screen.findByTestId("approvals-clear-filters"));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe(routes.approvals({ status: "decided" }));
    expect(screen.queryByTestId("approvals-clear-filters")).toBeNull();
  });

  it("an unknown kind or status in the URL falls back to the defaults", async () => {
    nextRouter.reset(["/approvals?status=bogus&kind=nope"]);
    render(<ApprovalsPage />);
    await screen.findByTestId("approvals-list");
    const q = lastList();
    expect(q.get("status")).toBe("pending");
    expect(q.has("kind")).toBe(false);
  });

  it("empty states differ for pending, decided and filtered", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) =>
      String(input).includes("/agents") ? jsonResponse([]) : jsonResponse({ items: [], next_cursor: null }));
    const { unmount } = render(<ApprovalsPage />);
    expect(await screen.findByText("Nothing is waiting for approval.")).toBeInTheDocument();
    unmount();
    nextRouter.reset([routes.approvals({ status: "decided" })]);
    const second = render(<ApprovalsPage />);
    expect(await screen.findByText("No decided approvals yet.")).toBeInTheDocument();
    second.unmount();
    nextRouter.reset([routes.approvals({ kind: "step" })]);
    render(<ApprovalsPage />);
    expect(await screen.findByText("No approvals match these filters.")).toBeInTheDocument();
  });

  it("Load more follows next_cursor and appends without duplicates", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/agents")) return jsonResponse([]);
      if (url.includes("cursor=c1")) return jsonResponse({ items: [toolItem, stepItem], next_cursor: null });
      return jsonResponse({ items: [toolItem], next_cursor: "c1" });
    });
    render(<ApprovalsPage />);
    fireEvent.click(await screen.findByTestId("approvals-load-more"));
    await screen.findByTestId(`approval-row-${WF_RUN}:review`);
    expect(screen.getAllByTestId(`approval-row-${RUN}:call_1`)).toHaveLength(1);
    await waitFor(() => expect(screen.queryByTestId("approvals-load-more")).toBeNull());
  });
});
