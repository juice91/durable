import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatTab } from "@/components/agentPage/ChatTab";
import { api, ApiError, type AgentSpec, type AgentVersion } from "@/lib/api";
import { getConversation, listConversations, type ConversationRun } from "@/lib/conversations";
import { listEvents, subscribeRun, type RunEvent, type RunEventData, type SubscribeHandlers } from "@/lib/runs";
import { decideToolApproval, refreshPendingApprovals } from "@/lib/approvals";
import { fetchModels } from "@/lib/metrics";
import { getRequestContext } from "@/lib/requestContext";
import { routes } from "@/lib/routes";

// AC.14 tester: Chat-tab streaming markers, fallback badge, limit errors, inline approval card.

vi.mock("next/navigation", () => ({
  usePathname: () => null,
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { list: vi.fn(), get: vi.fn() }, versions: { list: vi.fn() } } };
});
vi.mock("@/components/chat/turnApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/chat/turnApi")>()),
  startTurnWithOptions: vi.fn(),
}));
vi.mock("@/lib/metrics", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/metrics")>()), fetchModels: vi.fn() }));
vi.mock("@/lib/requestContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/requestContext")>()),
  getRequestContext: vi.fn(),
  validateRequestContext: vi.fn(),
}));
vi.mock("@/lib/conversations", () => ({
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  listConversations: vi.fn(),
  renameConversation: vi.fn(),
}));
vi.mock("@/lib/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/runs")>()),
  getRun: vi.fn(),
  listEvents: vi.fn(),
  subscribeRun: vi.fn(),
}));
vi.mock("@/lib/approvals", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/approvals")>()),
  decideToolApproval: vi.fn(),
  refreshPendingApprovals: vi.fn(),
}));

const SCHEMA = { type: "object", properties: { priority: { type: "string", enum: ["low", "high"] }, summary: { type: "string" } }, required: ["priority"] };
const structuredSpec = { spec_version: 1, name: "Triage", instructions: "Triage", model: { provider: "test", name: "structured" }, tools: [], output: { json_schema: SCHEMA } } as unknown as AgentSpec;
const agent = { id: "agent-1", name: "Triage", draft_spec: structuredSpec, latest_version: 1 };
const version: AgentVersion = { agent_id: "agent-1", version: 1, spec: structuredSpec, spec_hash: "h", created_at: "2026-11-15T00:00:00Z", created_by: "dev" };

const ev = (seq: number, type: RunEvent["type"], data: unknown): RunEvent => ({ seq, type, data: data as RunEventData, ts: "2026-11-15T00:00:00Z" });

const turnRun = (runId: string, display: ConversationRun["display_status"]): ConversationRun => ({
  run_id: runId,
  agent_id: "agent-1",
  version: 1,
  status: display === "waiting_approval" ? "running" : display,
  display_status: display,
  input: "hi",
  output: null,
  error: null,
  heartbeat_at: null,
  created_at: "2026-11-15T00:00:00Z",
  updated_at: "2026-11-15T00:00:00Z",
  trace_id: null,
  conversation_id: "conv-1",
  turn: 1,
});

let handlers: SubscribeHandlers | null = null;

function openConversation(display: ConversationRun["display_status"], events: RunEvent[]) {
  vi.mocked(getConversation).mockResolvedValue({
    id: "conv-1",
    agent_id: "agent-1",
    title: "Chat",
    turn_count: 1,
    created_at: "2026-11-15T00:00:00Z",
    updated_at: "2026-11-15T00:00:00Z",
    runs: [turnRun("run-1", display)],
  });
  vi.mocked(listEvents).mockResolvedValue(events);
  window.history.replaceState({}, "", routes.agentChat("agent-1", { conversation: "conv-1" }));
  return render(<ChatTab agentId="agent-1" conversationId="conv-1" />);
}

const retrieveEvents = (seq: number): RunEvent[] => [
  ev(seq, "tool_call", { tool_call_id: "r1", tool_name: "retrieve", args: { query: "disk" } }),
  ev(seq + 1, "tool_result", {
    tool_call_id: "r1",
    tool_name: "retrieve",
    status: "ok",
    content: [],
    sources: [{ n: 1, numbering: "run", title: "Disk guide", url: "https://docs.example.com/disk" }],
  }),
];

const waitingEvents: RunEvent[] = [
  ev(0, "run_started", { input: "open a ticket" }),
  ev(1, "model_message", { model_request: 0, text: "" }),
  ev(2, "tool_call", { tool_call_id: "c1", tool_name: "tickets.create", args: { title: "Disk full" } }),
  ev(3, "tool_approval_waiting", { tool_call_id: "c1", tool_name: "tickets.create", args: { title: "Disk full" }, deadline: "2099-01-01T00:00:00Z", workflow_id: "run-1" }),
];

beforeEach(() => {
  vi.clearAllMocks();
  handlers = null;
  window.sessionStorage.clear();
  window.localStorage.clear();
  vi.mocked(api.agents.get).mockResolvedValue(agent as never);
  vi.mocked(api.versions.list).mockResolvedValue([version]);
  vi.mocked(fetchModels).mockResolvedValue([]);
  vi.mocked(getRequestContext).mockResolvedValue({ agent_id: "agent-1", version: 1, draft: false, fields: [], schema: { type: "object", properties: {} } });
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null });
  vi.mocked(refreshPendingApprovals).mockResolvedValue(undefined);
  vi.mocked(subscribeRun).mockImplementation((_id, next) => {
    handlers = next;
    return () => undefined;
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
afterEach(() => vi.unstubAllGlobals());


describe("streaming markers", () => {
  it("shows the generating state, then a retry marker, then the structured answer replaces it", async () => {
    openConversation("running", [ev(0, "run_started", { input: "triage" })]);
    expect(await screen.findByTestId("turn-generating-structured")).toBeInTheDocument();
    act(() => handlers?.onEvent?.(ev(1, "model_message", { model_request: 0, text: '{"priority": 1}', output_candidate: true })));
    act(() => handlers?.onEvent?.(ev(2, "output_retry", { model_request: 0, errors: ["priority", "tags[0]"] })));
    expect(await screen.findByTestId("output-retry")).toHaveTextContent("(at priority, tags[0])");
    expect(screen.getByTestId("turn-generating-structured")).toBeInTheDocument();
    // The rejected candidate's raw JSON is never shown as chat text.
    expect(screen.queryByText(/"priority": 1/)).toBeNull();
    act(() => {
      handlers?.onEvent?.(ev(3, "run_completed", { output: '{"priority":"high"}', output_json: { priority: "high" } }));
      handlers?.onStatus?.({ display_status: "completed" });
    });
    expect(await screen.findByTestId("structured-output")).toBeInTheDocument();
    expect(screen.queryByTestId("turn-generating-structured")).toBeNull();
  });

  it("does not show the generating state while the turn waits for approval", async () => {
    openConversation("waiting_approval", waitingEvents);
    await screen.findByTestId("approval-tool-c1");
    expect(screen.queryByTestId("turn-generating-structured")).toBeNull();
  });
});

describe("fallback badge and limit errors", () => {
  const fb = (extra: Record<string, unknown> = {}) =>
    ev(1, "model_message", { model_request: 0, text: "hello", requested_model_id: "test:fail-503", fallback_index: 1, failed_attempts: [{ model_id: "test:fail-503", error_type: "ModelHTTPError", http_status: 503 }, { model_id: "test:fail-429", error_type: "ModelHTTPError", http_status: 429 }], ...extra });

  it("lists every failed attempt of the chain", async () => {
    openConversation("completed", [ev(0, "run_started", { input: "x" }), fb(), ev(2, "run_completed", { output: "hello" })]);
    expect(await screen.findByTestId("turn-fallback-attempts")).toHaveTextContent("2 failed attempts: test:fail-503 (ModelHTTPError, HTTP 503); test:fail-429 (ModelHTTPError, HTTP 429)");
  });

  it("names the answering model when the event carries answering_model", async () => {
    openConversation("completed", [ev(0, "run_started", { input: "x" }), fb({ answering_model: "test:backup-model" }), ev(2, "run_completed", { output: "hello" })]);
    const badge = await screen.findByTestId("turn-badge-fallback");
    expect(badge.textContent + (screen.queryByTestId("turn-fallback-attempts")?.textContent ?? "")).toContain("test:backup-model");
  });

  it("shows no fallback badge for a primary-only turn", async () => {
    openConversation("completed", [ev(0, "run_started", { input: "x" }), ev(1, "model_message", { model_request: 0, text: "hi", requested_model_id: "test:echo", fallback_index: 0 }), ev(2, "run_completed", { output: "hi" })]);
    await screen.findByText("hi");
    expect(screen.queryByTestId("turn-badge-fallback")).toBeNull();
  });

  it("shows the fallback-off badge when the override disabled fallback", async () => {
    openConversation("completed", [ev(0, "run_started", { input: "x", fallback: "disabled_by_override" }), ev(1, "run_completed", { output: "hi" })]);
    expect(await screen.findByTestId("turn-badge-fallback-off")).toBeInTheDocument();
  });

  it.each([
    [{ error_code: "usage_limit_exceeded", limit: "max_total_tokens", value: 12000 }, "Usage limit exceeded: max total tokens (12,000)."],
    [{ error_code: "usage_limit_exceeded", limit: "max_cost_usd", value: 0.5 }, "Usage limit exceeded: max cost (0.5)."],
    [{ error_code: "run_budget_exceeded", value: "0.01", spent_usd: "0.012" }, "The run reached its cost limit of $0.01 (spent $0.012)."],
    [{ error_code: "fallback_exhausted", cause: "provider_error" }, "first failure: provider_error"],
    [{ error_code: "output_mode_unsupported" }, "does not support native structured output"],
    [{ error_code: "output_invalid" }, "did not match the agent's output schema"],
  ])("shows the capability failure %j", async (data, text) => {
    openConversation("failed", [ev(0, "run_started", { input: "x" }), ev(1, "run_failed", { status: "failed", error: "raw provider text", ...data })]);
    const el = await screen.findByTestId("turn-capability-error");
    expect(el).toHaveTextContent(text);
    expect(el).toHaveAttribute("data-code", data.error_code);
    expect(screen.queryByText("raw provider text")).toBeNull();
  });
});

describe("inline approval card", () => {
  const ok = { run_id: "run-1", tool_call_id: "c1", decision: "approved", decided_by: "dev", comment: null, notified: true };

  it("sends the comment with a deny and the turn shows the denial", async () => {
    vi.mocked(decideToolApproval).mockResolvedValue({ ...ok, decision: "denied", comment: "too risky" } as never);
    openConversation("waiting_approval", waitingEvents);
    const card = await screen.findByTestId("approval-tool-c1");
    fireEvent.change(within(card).getByRole("textbox"), { target: { value: "too risky" } });
    fireEvent.click(within(card).getByRole("button", { name: "Deny" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(decideToolApproval).toHaveBeenCalledWith("run-1", "c1", "deny", "too risky"));
    act(() => {
      handlers?.onEvent?.(ev(4, "tool_denied", { tool_call_id: "c1", decided_by: "dev", comment: "too risky" }));
      handlers?.onEvent?.(ev(5, "tool_result", { tool_call_id: "c1", tool_name: "tickets.create", status: "denied", content: "denied" }));
      handlers?.onStatus?.({ display_status: "running" });
    });
    const decision = await screen.findByTestId("tool-approval-decision");
    expect(decision).toHaveAttribute("data-decision", "denied");
    expect(decision).toHaveTextContent("too risky");
  });

  it("re-reads the events after a 409 and the card gives way to the recorded decision", async () => {
    vi.mocked(decideToolApproval).mockRejectedValue(new ApiError(409, "already decided"));
    openConversation("waiting_approval", waitingEvents);
    const card = await screen.findByTestId("approval-tool-c1");
    const reads = vi.mocked(listEvents).mock.calls.length;
    vi.mocked(listEvents).mockResolvedValue([...waitingEvents, ev(4, "tool_approved", { tool_call_id: "c1", decided_by: "other" })]);
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(vi.mocked(listEvents).mock.calls.length).toBeGreaterThan(reads));
    expect(refreshPendingApprovals).toHaveBeenCalled();
    expect(await screen.findByTestId("tool-approval-decision")).toHaveAttribute("data-decision", "approved");
  });

  it("the header leaves 'Waiting for approval' once the call is decided (before the status frame); the composer re-enables when the turn completes", async () => {
    openConversation("waiting_approval", waitingEvents);
    await screen.findByTestId("approval-tool-c1");
    expect(screen.getByTestId("chat-run-status")).toHaveTextContent("Waiting for approval");
    // The decision event alone (the status frame still says waiting_approval): the turn reads as running.
    act(() => {
      handlers?.onEvent?.(ev(4, "tool_approved", { tool_call_id: "c1", decided_by: "dev" }));
    });
    await waitFor(() => expect(screen.getByTestId("chat-run-status")).toHaveTextContent("Running"));
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    act(() => {
      handlers?.onEvent?.(ev(5, "model_message", { model_request: 1, text: "Ticket created." }));
      handlers?.onEvent?.(ev(6, "run_completed", { output: "Ticket created." }));
      handlers?.onStatus?.({ display_status: "completed" });
    });
    await waitFor(() => expect(screen.getByTestId("chat-run-status")).not.toHaveTextContent("Waiting for approval"));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
  });

  it("shows a timed-out decision as a denial", async () => {
    openConversation("running", [...waitingEvents, ev(4, "tool_approval_timed_out", { tool_call_id: "c1" })]);
    expect(await screen.findByTestId("tool-approval-decision")).toHaveAttribute("data-decision", "timed_out");
  });
});
