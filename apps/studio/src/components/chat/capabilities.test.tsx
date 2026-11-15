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
import { runToThread } from "./thread";
import {
  turnCapabilityFailure,
  turnFallback,
  toolApprovals,
  STRUCTURED_OUTPUT_PART,
  OUTPUT_RETRY_PART,
  TOOL_APPROVAL_PART,
} from "./turnCapabilities";
import { toolCallInfos } from "./toolCalls";

// AC.14: the agent capabilities milestone in the Chat tab (structured output, retries, fallback,
// limits, tool approval) and the event helpers behind it.

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

describe("event helpers", () => {
  it("names the limit, the cost cap and the fallback cause", () => {
    expect(turnCapabilityFailure([ev(0, "run_failed", { error_code: "usage_limit_exceeded", limit: "max_requests", value: 2 })])?.message).toBe(
      "Usage limit exceeded: max requests (2).",
    );
    expect(turnCapabilityFailure([ev(0, "run_failed", { error_code: "run_budget_exceeded", limit: "max_cost_usd", value: "0.01", spent_usd: "0.012" })])?.message).toBe(
      "The run reached its cost limit of $0.01 (spent $0.012).",
    );
    expect(turnCapabilityFailure([ev(0, "run_failed", { error_code: "fallback_exhausted", cause: "provider_error" })])?.message).toContain("first failure: provider_error");
    expect(turnCapabilityFailure([ev(0, "run_failed", { error_code: "input_blocked" })])).toBeNull();
  });

  it("collects fallback attempts and approvals with their decisions", () => {
    const fallback = turnFallback([
      ev(0, "model_message", { model_request: 0, requested_model_id: "test:fail-503", fallback_index: 1, failed_attempts: [{ model_id: "test:fail-503", outcome: "error", error_type: "ModelHTTPError", http_status: 503 }] }),
    ]);
    expect(fallback).toEqual({ answeredByFallback: 1, primary: "test:fail-503", answeringModel: null, attempts: [{ modelId: "test:fail-503", outcome: "error", errorType: "ModelHTTPError", httpStatus: 503 }] });
    expect(turnFallback([ev(0, "model_message", { model_request: 0, text: "x" })])).toBeNull();
    const decided = toolApprovals([...waitingEvents, ev(4, "tool_denied", { tool_call_id: "c1", decided_by: "dev", comment: "no" })]);
    expect(decided[0].decision).toEqual({ kind: "denied", decidedBy: "dev", comment: "no" });
  });

  it("marks a waiting call and a denied call in the tool card statuses", () => {
    expect(toolCallInfos(waitingEvents, true).get("c1")?.status).toBe("waiting");
    const denied = [...waitingEvents, ev(4, "tool_approval_timed_out", { tool_call_id: "c1" }), ev(5, "tool_result", { tool_call_id: "c1", status: "denied", content: "denied" })];
    expect(toolCallInfos(denied, true).get("c1")?.status).toBe("denied");
  });

  it("turns a structured turn into data parts and hides the candidate's raw JSON text", () => {
    const { messages } = runToThread(
      [
        ev(0, "run_started", { input: "triage" }),
        ev(1, "model_message", { model_request: 0, text: '{"priority": 1}', output_candidate: true }),
        ev(2, "output_retry", { model_request: 0, errors: ["priority"] }),
        ev(3, "model_message", { model_request: 1, text: '{"priority":"high"}', output_candidate: true }),
        ev(4, "run_completed", { output: '{"priority":"high"}', output_json: { priority: "high" } }),
      ],
      {},
      "run-1:",
    );
    const parts = messages
      .flatMap((m) => m.content as ReadonlyArray<{ type: string; name?: string }>)
      .map((p) => (p.type === "data" ? `data:${p.name}` : p.type));
    expect(parts).toEqual(["text", `data:${OUTPUT_RETRY_PART}`, `data:${STRUCTURED_OUTPUT_PART}`]);
    expect(JSON.stringify(messages)).not.toContain('\\"priority\\": 1');
  });

  it("puts the approval card after the gated call", () => {
    const { messages } = runToThread(waitingEvents, {}, "run-1:");
    const last = messages[messages.length - 1];
    expect(last.id).toBe("run-1:assistant-approval-c1");
    expect(last.content[0]).toMatchObject({ type: "data", name: TOOL_APPROVAL_PART, data: { toolCallId: "c1", decision: null } });
  });
});

describe("Chat tab: agent capabilities", () => {
  it("renders the structured answer with [n] linked to the retrieve source, never to a URL in the output", async () => {
    openConversation("completed", [
      ev(0, "run_started", { input: "triage" }),
      ev(1, "model_message", { model_request: 0, text: "" }),
      ...retrieveEvents(2),
      ev(4, "model_message", { model_request: 1, text: "", output_candidate: true }),
      ev(5, "run_completed", {
        output: "{}",
        output_json: { priority: "high", summary: "Disk is full [1]. See [2](https://evil.example) and https://evil.example/x", citations: [1] },
      }),
    ]);
    const view = await screen.findByTestId("structured-output");
    const links = within(view).getAllByTestId("citation-link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://docs.example.com/disk", "https://docs.example.com/disk"]);
    expect(within(view).queryByRole("link", { name: "[2]" })).toBeNull();
    expect(within(view).getByText("high")).toBeInTheDocument();
    fireEvent.click(within(view).getByTestId("structured-output-raw-toggle"));
    expect(within(view).getByTestId("structured-output-json").textContent).toContain('"priority": "high"');
  });

  it("shows the retry marker, the fallback badge with its attempts, and the limit error", async () => {
    openConversation("failed", [
      ev(0, "run_started", { input: "triage" }),
      ev(1, "model_message", { model_request: 0, text: "", output_candidate: true, requested_model_id: "test:fail-503", fallback_index: 1, failed_attempts: [{ model_id: "test:fail-503", error_type: "ModelHTTPError", http_status: 503 }] }),
      ev(2, "output_retry", { model_request: 0, errors: ["priority"] }),
      ev(3, "run_failed", { status: "failed", error: "the run exceeded a usage limit", error_code: "usage_limit_exceeded", limit: "max_requests", value: 2 }),
    ]);
    expect(await screen.findByTestId("output-retry")).toHaveTextContent("(at priority)");
    expect(screen.getByTestId("turn-badge-fallback")).toHaveTextContent("fallback");
    expect(screen.getByTestId("turn-fallback-attempts")).toHaveTextContent("test:fail-503 (ModelHTTPError, HTTP 503)");
    expect(screen.getByTestId("turn-capability-error")).toHaveTextContent("Usage limit exceeded: max requests (2).");
    // Shown once, under the turn: not repeated in the header.
    expect(screen.queryByText("the run exceeded a usage limit")).toBeNull();
  });

  it("shows 'Generating structured output…' while a structured turn runs", async () => {
    openConversation("running", [ev(0, "run_started", { input: "triage" })]);
    expect(await screen.findByTestId("turn-generating-structured")).toHaveTextContent("Generating structured output…");
  });

  it("shows the approval card again on reload, approves, and the stream continues in place", async () => {
    vi.mocked(decideToolApproval).mockResolvedValue({ run_id: "run-1", tool_call_id: "c1", decision: "approved", decided_by: "dev", comment: null, notified: true } as never);
    openConversation("waiting_approval", waitingEvents);
    const card = await screen.findByTestId("approval-tool-c1");
    expect(within(card).getByTestId("approval-args-tool-c1")).toHaveTextContent('"title": "Disk full"');
    expect(screen.getByTestId("chat-run-status")).toHaveTextContent("Waiting for approval");
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(decideToolApproval).toHaveBeenCalledWith("run-1", "c1", "approve", ""));
    expect(refreshPendingApprovals).toHaveBeenCalled();
    act(() => {
      handlers?.onEvent?.(ev(4, "tool_approved", { tool_call_id: "c1", decided_by: "dev" }));
      handlers?.onStatus?.({ display_status: "running" });
    });
    const decision = await screen.findByTestId("tool-approval-decision");
    expect(decision).toHaveAttribute("data-decision", "approved");
    expect(screen.queryByTestId("approval-tool-c1")).toBeNull();
  });

  it("denies after a confirm, and explains a 409 race", async () => {
    vi.mocked(decideToolApproval).mockRejectedValue(new ApiError(409, "tool call is already decided"));
    openConversation("waiting_approval", waitingEvents);
    const card = await screen.findByTestId("approval-tool-c1");
    fireEvent.click(within(card).getByRole("button", { name: "Deny" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(decideToolApproval).toHaveBeenCalledWith("run-1", "c1", "deny", ""));
    expect(await screen.findByTestId("approval-error-tool-c1")).toHaveTextContent("no longer waiting for approval");
  });

  it("explains a lost race after the card turns into the recorded decision", async () => {
    vi.mocked(decideToolApproval).mockRejectedValue(new ApiError(409, "tool call is already decided"));
    openConversation("waiting_approval", waitingEvents);
    const card = await screen.findByTestId("approval-tool-c1");
    vi.mocked(listEvents).mockResolvedValue([...waitingEvents, ev(4, "tool_approved", { tool_call_id: "c1", decided_by: "other" })]);
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    const decision = await screen.findByTestId("tool-approval-decision");
    expect(decision).toHaveTextContent("Approved by other");
    expect(within(decision).getByTestId("tool-approval-lost-race")).toHaveTextContent("Your decision was not recorded");
  });
});
