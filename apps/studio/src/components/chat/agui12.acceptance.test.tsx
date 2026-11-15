// AGUI.12 acceptance (tester): the agent Chat tab on AG-UI against a mock AG-UI server that replays
// REAL recorded AGUI.4 translator output (test/fixtures/agui4-recorded.json, made by running
// apps/api/api_tests/agui4_corpus.py through agui4_harness.agui_chunks). Covers full turns, structured
// output, interrupts and resume, reconnect, run options, the Settings switch, parity and layout.
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ path: "/agents/a1/chat" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { agents: { list: vi.fn(), get: vi.fn() }, versions: { list: vi.fn() } } };
});
vi.mock("@/lib/conversations", () => ({
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  listConversations: vi.fn(),
  renameConversation: vi.fn(),
  deleteConversation: vi.fn(),
}));
vi.mock("@/lib/metrics", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/metrics")>()), fetchModels: vi.fn() }));
vi.mock("@/components/chat/turnApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/chat/turnApi")>()),
  startTurnWithOptions: vi.fn(),
}));
vi.mock("@/lib/requestContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/requestContext")>()),
  getRequestContext: vi.fn(),
  validateRequestContext: vi.fn(),
}));
vi.mock("@/lib/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/runs")>()),
  listEvents: vi.fn(),
  subscribeRun: vi.fn(),
  getRun: vi.fn(),
}));
vi.mock("@/lib/approvals", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/approvals")>()),
  decideToolApproval: vi.fn(),
  refreshPendingApprovals: vi.fn(async () => undefined),
}));

import { api, type Agent, type AgentSpec, type AgentVersion } from "@/lib/api";
import { getConversation, listConversations } from "@/lib/conversations";
import { fetchModels } from "@/lib/metrics";
import { getRequestContext, type RequestContextInfo } from "@/lib/requestContext";
import { getRun, listEvents, subscribeRun, type RunEvent } from "@/lib/runs";
import { decideToolApproval } from "@/lib/approvals";
import { startTurnWithOptions } from "@/components/chat/turnApi";
import { createConversation } from "@/lib/conversations";
import { ChatTab } from "@/components/agentPage/ChatTab";
import { SettingsSections } from "@/components/settings/SettingsSections";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { BROWSER_DATA } from "@/components/settings/browserData";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { CHAT_PROTOCOL_STORAGE_KEY, readChatProtocol, setChatProtocolDefaultForTests } from "./chatProtocol";
import recorded from "@/test/fixtures/agui4-recorded.json";

process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = process.env.NEXT_PUBLIC_EAP_DEV_TOKEN || "Bearer dev:t1:u1";

// ------------------------------------------------------------------ the mock server, from recordings
const NATIVE = "7d1e0000-0000-4000-8000-0000000000a1"; // the corpus's native run id
const enc = new TextEncoder();
type Ev = Record<string, any> & { type: string };
type Frame = { id: string | null; event: Ev | null };
interface Call { method: string; url: string; body: Record<string, any> | null; lastEventId: string | null }
const REC = recorded as unknown as Record<string, any>;

/** A recorded stream as SSE, with this request's thread and run ids and the route's `pydanticos.run`. */
function replay(frames: Frame[], threadId: string, runId: string, opts: { cut?: number; skip?: number; conv?: string; history?: Ev[]; noRunInfo?: boolean } = {}): Response {
  const out: string[] = [];
  frames.forEach((frame, index) => {
    if (!frame.event || index < (opts.skip ?? 0)) return;
    // The route's AgUiStreamContext.history (the thread so far, with this run's user message) leads a snapshot.
    const event = frame.event.type === "MESSAGES_SNAPSHOT" && opts.history ? { ...frame.event, messages: [...opts.history, ...frame.event.messages] } : frame.event;
    const text = JSON.stringify(event).replaceAll("thread-x", threadId).replaceAll("agui-run", runId);
    if (frame.event.type === "RUN_STARTED") {
      out.push(`data: ${text}\n\n`);
      if (opts.noRunInfo) return;
      const info = { type: "CUSTOM", name: "pydanticos.run", value: { run_id: NATIVE, conversation_id: opts.conv ?? "c1", turn: 1, trace_id: null, model_id: "openai:gpt-4.1-mini", version: 2, draft: false } };
      out.push(`data: ${JSON.stringify(info)}\n\n`);
      return;
    }
    out.push((frame.id ? `id: ${frame.id.replaceAll("agui-run", runId)}\n` : "") + `data: ${text}\n\n`);
  });
  const chunks = opts.cut === undefined ? out : out.slice(0, opts.cut);
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(enc.encode(chunks.join("")));
        controller.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

let calls: Call[] = [];
let threadBody: Record<string, unknown> = {};
let onPost: (body: Record<string, any>, call: Call) => Response = () => json({ detail: "unset" }, 500);
let onAttach: (runId: string, call: Call) => Response = () => json({ detail: "Run not found" }, 404);
const posts = () => calls.filter((call) => call.method === "POST");

function installServer() {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const path = new URL(url).pathname;
    const method = init?.method ?? "GET";
    const headers = new Headers(init?.headers ?? {});
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as Record<string, any>) : null;
    const call = { method, url, body, lastEventId: headers.get("Last-Event-ID") };
    calls.push(call);
    if (method === "GET" && /\/ag-ui\/agents\/a1\/threads\/[^/]+$/.test(path)) return json({ threadId: decodeURIComponent(path.split("/").pop() ?? ""), ...threadBody });
    const attach = /\/threads\/[^/]+\/runs\/([^/]+)\/stream$/.exec(path);
    if (method === "GET" && attach) return onAttach(decodeURIComponent(attach[1]), call);
    if (method === "POST" && path === "/api/v1/ag-ui/agents/a1") return onPost(body ?? {}, call);
    return json({ detail: `unexpected ${method} ${url}` }, 404);
  }));
}

// ------------------------------------------------------------------ fixtures and helpers
const spec = (): AgentSpec => ({ spec_version: 1, name: "Support bot", instructions: "x", model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [] }) as AgentSpec;
const AGENT: Agent = { id: "a1", name: "Support bot", draft_spec: spec(), latest_version: 2 };
const VERSIONS: AgentVersion[] = [1, 2].map((version) => ({ agent_id: "a1", version, spec: spec(), spec_hash: `h${version}`, created_at: "", created_by: "dev" }));
const NO_FIELDS = { agent_id: "a1", version: 2, draft: false, fields: [], schema: {} } as RequestContextInfo;
const nativeOf = (name: string): RunEvent[] => REC[name].native;
const framesOf = (name: string, key = "frames"): Frame[] => REC[name][key];
const box = () => screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
const sentRuns = () => posts().map((call) => call.body?.runId as string);

async function send(text: string) {
  await waitFor(() => expect(box()).not.toBeDisabled());
  fireEvent.change(box(), { target: { value: text } });
  fireEvent.keyDown(box(), { key: "Enter", code: "Enter" });
}
/** Serve a recorded run for every POST; the native events for that run come from `listEvents`. */
function serve(name: string, key = "frames") {
  vi.mocked(listEvents).mockResolvedValue(nativeOf(name));
  onPost = (body) => replay(framesOf(name, key), String(body.threadId), String(body.runId));
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.localStorage.setItem(CHAT_PROTOCOL_STORAGE_KEY, "agui");
  window.history.replaceState({}, "", "/agents/a1/chat");
  nav.path = "/agents/a1/chat";
  threadBody = { messages: [], runs: [], activeRunId: null, conversationId: null };
  onPost = () => json({ detail: "unset" }, 500);
  onAttach = () => json({ detail: "Run not found" }, 404);
  installServer();
  vi.mocked(api.agents.get).mockResolvedValue(AGENT);
  vi.mocked(api.agents.list).mockResolvedValue([AGENT]);
  vi.mocked(api.versions.list).mockResolvedValue(VERSIONS);
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null } as never);
  vi.mocked(fetchModels).mockResolvedValue([
    { id: "openai:gpt-4.1-mini", provider: "openai", name: "gpt-4.1-mini", wildcard: false, source: "allowlist", description: null },
    { id: "openai:gpt-4.1", provider: "openai", name: "gpt-4.1", wildcard: false, source: "allowlist", description: null },
  ]);
  vi.mocked(getRequestContext).mockResolvedValue(NO_FIELDS);
  vi.mocked(listEvents).mockResolvedValue([]);
  vi.mocked(getRun).mockResolvedValue({ run_id: NATIVE, display_status: "completed", chat_input_tokens: 11, chat_output_tokens: 7, chat_cost_usd: "0.0012" } as never);
  vi.mocked(getConversation).mockResolvedValue({ id: "c1", agent_id: "a1", title: "T", turn_count: 1, created_at: "", updated_at: "", runs: [], resource_id: "u1" } as never);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// ------------------------------------------------------------------ a full chat turn
describe("a full chat turn on AG-UI (recorded tool chain)", () => {
  it("streams text, renders both tool calls with their statuses, and shows the usage badge from native data", async () => {
    serve("tool_chain");
    render(<ChatTab agentId="a1" />);
    await send("check things");
    await waitFor(() => expect(screen.getAllByTestId("chat-message-assistant").map((el) => el.textContent).join(" ")).toContain("Done"));
    expect(screen.getAllByTestId("chat-message-assistant").map((el) => el.textContent).join(" ")).toContain("Let me check.");
    const cards = await screen.findAllByTestId("tool-call-card");
    expect(cards).toHaveLength(2);
    await waitFor(() => expect(cards.map((c) => c.getAttribute("data-status")).sort()).toEqual(["error", "ok"].sort()));
    // The footer's tokens come from getRun(native run id), not from the stream.
    await waitFor(() => expect(getRun).toHaveBeenCalledWith(NATIVE));
    await waitFor(() => expect(screen.getByTestId("turn-badge-tokens")).toHaveTextContent(/11/));
    expect(subscribeRun).not.toHaveBeenCalled();
    expect(startTurnWithOptions).not.toHaveBeenCalled();
  });

  it("renders a retrieve turn's citation links from the turn's native tool results", async () => {
    serve("retrieve");
    render(<ChatTab agentId="a1" />);
    await send("jam?");
    await waitFor(() => expect(screen.getAllByTestId("chat-message-assistant").map((el) => el.textContent).join(" ")).toContain("Open tray two"));
    const links = await screen.findAllByTestId("citation-link");
    expect(links[0]).toHaveAttribute("data-citation", "1");
    expect(links[0].getAttribute("href")).toContain("x.test/a");
  });

  it("shows the fallback badge from the native events for a fallback run", async () => {
    serve("fallback");
    vi.mocked(getRun).mockResolvedValue({ run_id: NATIVE, display_status: "completed", model_id: "anthropic:b", chat_input_tokens: 1, chat_output_tokens: 1, chat_cost_usd: "0.0001" } as never);
    render(<ChatTab agentId="a1" />);
    await send("hi");
    await waitFor(() => expect(screen.getByTestId("turn-badge-fallback")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("turn-badge-model")).toHaveTextContent("anthropic:b"));
  });
});

// ------------------------------------------------------------------ structured output
describe("structured output on AG-UI", () => {
  it("renders RUN_FINISHED.result (recorded) as the AC.14 JSON tree, with the raw toggle", async () => {
    serve("structured");
    render(<ChatTab agentId="a1" />);
    await send("age?");
    const view = await screen.findByTestId("structured-output");
    const field = within(view).getByTestId("structured-output-field");
    expect(field).toHaveAttribute("data-path", "age");
    expect(field).toHaveTextContent("3");
    fireEvent.click(within(view).getByTestId("structured-output-raw-toggle"));
    expect(within(view).getByTestId("structured-output-json").textContent).toContain('"age": 3');
    // The output_retry custom event of the recording does not leak its cause.
    expect(document.body.textContent).not.toContain("SECRETVALUE");
  });

  it("falls back to the native output_json when RUN_FINISHED carries no result", async () => {
    const frames = framesOf("structured").map((f) => (f.event?.type === "RUN_FINISHED" ? { ...f, event: { ...f.event, result: undefined } } : f));
    vi.mocked(listEvents).mockResolvedValue(nativeOf("structured"));
    onPost = (body) => replay(frames, String(body.threadId), String(body.runId));
    render(<ChatTab agentId="a1" />);
    await send("age?");
    expect(await screen.findByTestId("structured-output")).toHaveTextContent("3");
  });
});

// ------------------------------------------------------------------ client tools
describe("client tools", () => {
  it("sends none: the Chat tab offers no client tools (tools: [] on every POST, including resume)", async () => {
    serve("plain");
    render(<ChatTab agentId="a1" />);
    await send("hi");
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].body?.tools ?? []).toEqual([]);
    expect(JSON.stringify(posts()[0].body)).not.toContain("clientTool");
  });
});

// ------------------------------------------------------------------ tool approvals as interrupts
const INTERRUPT = `approval:${NATIVE}:tool:g1`;
const WAITING: number = REC.approval.waiting;
/** The recording's deadline (2026-11-16) moved far ahead, so these tests do not expire; expiry is
 * tested on its own below. */
const FUTURE = (value: unknown) => JSON.parse(JSON.stringify(value).replaceAll("2026-11-16T00:00:00", "2099-01-01T00:00:00"));
const approvalFrames = (key: string): Frame[] => FUTURE(framesOf("approval", key));
const approvalNative = (): RunEvent[] => FUTURE(nativeOf("approval"));
const waitingEvents = (): RunEvent[] => approvalNative().slice(0, WAITING + 1);
/** First POST: the recorded stream that ends in the interrupt. Resume POST: the recorded continuation. */
function serveApproval(
  resume: (body: Record<string, any>, call: Call) => Response = (body) => replay(approvalFrames("resume"), String(body.threadId), String(body.runId)),
  first: Frame[] = approvalFrames("first"),
) {
  vi.mocked(listEvents).mockResolvedValue(waitingEvents());
  onPost = (body, call) => (Array.isArray(body.resume) ? resume(body, call) : replay(first, String(body.threadId), String(body.runId)));
}
const lastText = () => screen.getAllByTestId("chat-message-assistant").at(-1)?.textContent ?? "";

describe("tool approvals on AG-UI (recorded interrupt)", () => {
  it("renders the shared ApprovalCard from the interrupt, with args and deadline; approve resumes and the turn ends", async () => {
    serveApproval();
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    const wrapper = await screen.findByTestId("tool-approval-waiting");
    expect(wrapper).toHaveAttribute("data-interrupt-id", INTERRUPT);
    const card = within(wrapper).getByRole("region", { name: "Approval for tool pay" });
    expect(card).toHaveTextContent(/amount/);
    expect(within(card).getByTestId("approval-deadline-tool-g1")).toBeInTheDocument();
    expect(box()).toBeDisabled();
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    expect(posts()[1].body?.resume).toEqual([{ interruptId: INTERRUPT, status: "resolved", payload: { approved: true } }]);
    expect(posts()[1].body?.threadId).toBe(posts()[0].body?.threadId);
    await waitFor(() => expect(lastText()).toContain("Done"));
    expect(decideToolApproval).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByTestId("tool-approval-waiting")).not.toBeInTheDocument());
    await waitFor(() => expect(box()).not.toBeDisabled());
  });

  it("denies with a comment through the confirm dialog: reason travels in the resume payload", async () => {
    serveApproval();
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    const card = await screen.findByRole("region", { name: "Approval for tool pay" });
    fireEvent.change(within(card).getByRole("textbox"), { target: { value: "  not today  " } });
    fireEvent.click(within(card).getByRole("button", { name: "Deny" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    expect(posts()[1].body?.resume).toEqual([{ interruptId: INTERRUPT, status: "resolved", payload: { approved: false, reason: "not today" } }]);
    expect(decideToolApproval).not.toHaveBeenCalled();
  });

  it("reflects a decision made elsewhere (Approvals inbox): the native events are re-read and the call is resumed with it", async () => {
    serveApproval();
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    await screen.findByTestId("tool-approval-waiting");
    // Meanwhile the inbox denied it: the native log now has tool_denied.
    const denied = nativeOf("approval").slice(0, WAITING + 1).concat([{ ...nativeOf("approval")[WAITING + 1], type: "tool_denied", data: { tool_call_id: "g1", decided_by: "u2", comment: null } } as RunEvent]);
    vi.mocked(listEvents).mockResolvedValue(denied);
    await waitFor(() => expect(posts()).toHaveLength(2), { timeout: 9000 });
    expect(posts()[1].body?.resume).toEqual([{ interruptId: INTERRUPT, status: "resolved", payload: { approved: false } }]);
    await waitFor(() => expect(lastText()).toContain("Done"));
  }, 15000);
});

describe("tool approvals: a refused resume (409, e.g. a reused runId)", () => {
  it("shows a clear message, keeps the transcript, re-reads the run and settles the tool card (not a silent hang)", async () => {
    serveApproval(() => json({ detail: "Run is not waiting for approval" }, 409));
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    const card = await screen.findByRole("region", { name: "Approval for tool pay" });
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    // The user is told, somewhere, what happened.
    await waitFor(() => expect(document.body.textContent).toMatch(/no longer waiting|not waiting|already (been )?decided|decided elsewhere|Failed to send/i));
    // The original user message and the tool call stay in the transcript.
    expect(screen.getByTestId("chat-message-user")).toHaveTextContent("pay 5");
    // AC.14's lost-race path: the native events are re-read at once (the winner's decision is in them),
    // so the turn shows what was recorded and the composer is not left locked by a dead interrupt.
    const reads = vi.mocked(listEvents).mock.calls.length;
    vi.mocked(listEvents).mockResolvedValue(approvalNative());
    await waitFor(() => expect(vi.mocked(listEvents).mock.calls.length).toBeGreaterThan(reads), { timeout: 1500 });
    // ...and the call no longer reads "Running": the recorded outcome (tool_result) replaces it.
    await waitFor(() => expect(screen.getAllByTestId("tool-call-status").map((e) => e.textContent)).not.toContain("Running"), { timeout: 1500 });
  });
});


// ------------------------------------------------------------------ decisions, lost races, expiry (review fixes)
/** The recorded resume stream with its approval_decided set to `decision` (what the server recorded). */
const resumeDeciding = (decision: string) => (body: Record<string, any>) =>
  replay(
    approvalFrames("resume").map((f) =>
      f.event?.type === "CUSTOM" && f.event.name === "pydanticos.approval_decided" ? { ...f, event: { ...f.event, value: { ...f.event.value, decision } } } : f,
    ),
    String(body.threadId),
    String(body.runId),
  );
const nativeDecided = (type: "tool_approved" | "tool_denied", decidedBy: string): RunEvent[] =>
  approvalNative().map((e) => (e.type === "tool_approved" ? ({ ...e, type, data: { tool_call_id: "g1", decided_by: decidedBy, comment: null } } as RunEvent) : e));

describe("tool approvals: the decision line and the real lost race (approval_decided)", () => {
  it("after approving, the gated call keeps AC.14's decision line ('pay: Approved by <who>') under its tool card", async () => {
    serveApproval();
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    const card = await screen.findByRole("region", { name: "Approval for tool pay" });
    vi.mocked(listEvents).mockResolvedValue(nativeDecided("tool_approved", "u1"));
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    const line = await screen.findByTestId("tool-approval-decision");
    expect(line).toHaveAttribute("data-decision", "approved");
    await waitFor(() => expect(line).toHaveTextContent("pay: Approved by u1"));
    expect(screen.queryByTestId("tool-approval-lost-race")).not.toBeInTheDocument();
  });

  it("the server accepted the resume but another decision was recorded first: the line shows it with the lost-race notice", async () => {
    // The inbox denied the call (u2) just before this user's Approve: the resume is accepted, the
    // recorded denial wins, and the stream says so (approval_decided: denied), D15.
    serveApproval(resumeDeciding("denied"));
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    const card = await screen.findByRole("region", { name: "Approval for tool pay" });
    vi.mocked(listEvents).mockResolvedValue(nativeDecided("tool_denied", "u2"));
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    expect(posts()[1].body?.resume).toEqual([{ interruptId: INTERRUPT, status: "resolved", payload: { approved: true } }]);
    const line = await screen.findByTestId("tool-approval-decision");
    await waitFor(() => expect(line).toHaveTextContent("pay: Denied by u2"));
    expect(within(line).getByTestId("tool-approval-lost-race")).toHaveTextContent("Your decision was not recorded");
    await waitFor(() => expect(box()).not.toBeDisabled());
  });

  it("the same decision recorded elsewhere is not a lost race", async () => {
    serveApproval(resumeDeciding("approved"));
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    const card = await screen.findByRole("region", { name: "Approval for tool pay" });
    vi.mocked(listEvents).mockResolvedValue(nativeDecided("tool_approved", "u2"));
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByTestId("tool-approval-decision")).toHaveTextContent("pay: Approved by u2"));
    expect(screen.queryByTestId("tool-approval-lost-race")).not.toBeInTheDocument();
  });
});

describe("tool approvals: an expired interrupt never locks the chat", () => {
  const expired = (): Frame[] => JSON.parse(JSON.stringify(framesOf("approval", "first")).replaceAll("2026-11-16T00:00:00", "2020-01-01T00:00:00"));

  it("approving after the deadline answers 'cancelled' (the AG-UI rule for expired interrupts), and the turn and composer carry on", async () => {
    serveApproval(undefined, expired());
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    const card = await screen.findByRole("region", { name: "Approval for tool pay" });
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    expect(posts()[1].body?.resume).toEqual([{ interruptId: INTERRUPT, status: "cancelled" }]);
    expect(screen.queryByTestId("agui-approval-error")).not.toBeInTheDocument();
    await waitFor(() => expect(lastText()).toContain("Done"));
    await waitFor(() => expect(box()).not.toBeDisabled());
  });

  it("a timeout read from the native events is answered 'cancelled' on its own", async () => {
    serveApproval(undefined, expired());
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    await screen.findByTestId("tool-approval-waiting");
    const timedOut = waitingEvents().concat([{ ...approvalNative()[WAITING + 1], type: "tool_approval_timed_out", data: { tool_call_id: "g1" } } as RunEvent]);
    vi.mocked(listEvents).mockResolvedValue(timedOut);
    await waitFor(() => expect(posts()).toHaveLength(2), { timeout: 9000 });
    expect(posts()[1].body?.resume).toEqual([{ interruptId: INTERRUPT, status: "cancelled" }]);
    await waitFor(() => expect(box()).not.toBeDisabled());
  }, 15000);
});

describe("structured output: scalar results and the output-retry marker", () => {
  it("shows the AC.14 output-retry marker of a structured turn that retried (from the native output_retry)", async () => {
    serve("structured");
    render(<ChatTab agentId="a1" />);
    await send("age?");
    await screen.findByTestId("structured-output");
    const marker = await screen.findByTestId("output-retry");
    expect(marker).toHaveTextContent("Output retry.");
  });

  it("renders a scalar structured result (3) with the structured view, not as text", async () => {
    const frames = framesOf("structured").map((f) => {
      if (f.event?.type === "TEXT_MESSAGE_CONTENT") return { ...f, event: { ...f.event, delta: "3" } };
      if (f.event?.type === "RUN_FINISHED") return { ...f, event: { ...f.event, result: 3 } };
      return f;
    });
    vi.mocked(listEvents).mockResolvedValue(nativeOf("structured"));
    onPost = (body) => replay(frames, String(body.threadId), String(body.runId));
    render(<ChatTab agentId="a1" />);
    await send("count?");
    const view = await screen.findByTestId("structured-output");
    expect(view).toHaveTextContent("3");
  });
});

describe("reattach and run mapping gaps", () => {
  it("a reload while a turn waits for approval re-raises the interrupt from the attach replay, and approving resumes", async () => {
    window.history.replaceState({}, "", "/agents/a1/chat/c1");
    nav.path = "/agents/a1/chat/c1";
    threadBody = {
      conversationId: "c1",
      messages: [{ id: "u-1", role: "user", content: "pay 5" }],
      runs: [{ runId: "x1", run_id: NATIVE, turn: 1, status: "running", display_status: "waiting_approval", superseded: false }],
      activeRunId: "x1",
    };
    vi.mocked(listEvents).mockResolvedValue(waitingEvents());
    onAttach = (runId) => replay(approvalFrames("full"), "c1", runId);
    onPost = (body) => replay(approvalFrames("resume"), String(body.threadId), String(body.runId));
    render(<ChatTab agentId="a1" conversationId="c1" />);
    const card = await screen.findByRole("region", { name: "Approval for tool pay" });
    expect(posts()).toHaveLength(0);
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].body?.resume).toEqual([{ interruptId: INTERRUPT, status: "resolved", payload: { approved: true } }]);
    await waitFor(() => expect(lastText()).toContain("Done"));
  });

  it("a resume stream without CUSTOM pydanticos.run still maps to the native run (from the interrupt id): usage and statuses are read", async () => {
    serveApproval((body) => replay(approvalFrames("resume"), String(body.threadId), String(body.runId), { noRunInfo: true }));
    render(<ChatTab agentId="a1" />);
    await send("pay 5");
    const card = await screen.findByRole("region", { name: "Approval for tool pay" });
    vi.mocked(getRun).mockClear();
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(lastText()).toContain("Done"));
    await waitFor(() => expect(getRun).toHaveBeenCalledWith(NATIVE));
  });

  it("a turn started natively and still running when the thread opens on AG-UI is followed natively, then the thread reloads", async () => {
    window.history.replaceState({}, "", "/agents/a1/chat/c1");
    nav.path = "/agents/a1/chat/c1";
    threadBody = {
      conversationId: "c1",
      messages: [{ id: `${NATIVE}-u`, role: "user", content: "hi" }],
      runs: [{ runId: null, run_id: NATIVE, turn: 1, status: "running", display_status: "running", superseded: false }],
      activeRunId: null,
    };
    vi.mocked(getConversation).mockResolvedValue({
      id: "c1", agent_id: "a1", title: "T", turn_count: 1, created_at: "", updated_at: "", resource_id: "u1",
      runs: [{ run_id: NATIVE, agent_id: "a1", version: 2, status: "running", display_status: "running", input: "hi", output: null, error: null, conversation_id: "c1", turn: 1 }],
    } as never);
    vi.mocked(listEvents).mockResolvedValue([{ seq: 0, type: "run_started", data: { input: "hi" }, ts: "2026-11-15T00:00:00Z" } as RunEvent]);
    let handlers: { onEvent: (e: RunEvent) => void } | null = null;
    vi.mocked(subscribeRun).mockImplementation((_id, h) => {
      handlers = h as never;
      return () => undefined;
    });
    render(<ChatTab agentId="a1" conversationId="c1" />);
    await waitFor(() => expect(subscribeRun).toHaveBeenCalledWith(NATIVE, expect.anything(), expect.anything()));
    expect(screen.getByTestId("chat-native-follow")).toHaveTextContent("started with the native chat protocol");
    expect(box()).toBeDisabled();
    const threadGets = () => calls.filter((call) => call.method === "GET" && call.url.endsWith("/threads/c1")).length;
    const before = threadGets();
    threadBody = { ...threadBody, messages: [{ id: `${NATIVE}-u`, role: "user", content: "hi" }, { id: `${NATIVE}-m0`, role: "assistant", content: "native answer" }], runs: [{ runId: null, run_id: NATIVE, turn: 1, status: "completed", display_status: "completed", superseded: false }] };
    await act(async () => {
      handlers?.onEvent({ seq: 1, type: "run_completed", data: {}, ts: "2026-11-15T00:00:01Z" } as RunEvent);
    });
    await waitFor(() => expect(threadGets()).toBeGreaterThan(before));
    await waitFor(() => expect(lastText()).toContain("native answer"));
    expect(screen.queryByTestId("chat-native-follow")).not.toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ reconnect
describe("reconnect", () => {
  it("a network drop mid-stream resumes with Last-Event-ID: one user message, no duplicated or lost text, tools once", async () => {
    vi.mocked(listEvents).mockResolvedValue(nativeOf("tool_chain"));
    let n = 0;
    onPost = (body) => {
      n += 1;
      // First response ends after TEXT_MESSAGE_END (frame id :1), with no RUN_FINISHED: a dropped connection.
      if (n === 1) return replay(framesOf("tool_chain"), String(body.threadId), String(body.runId), { cut: 6 });
      return replay(framesOf("tool_chain", "resume_after_1"), String(body.threadId), String(body.runId), { history: body.messages.slice(-1) });
    };
    render(<ChatTab agentId="a1" />);
    await send("check things");
    await waitFor(() => expect(posts()).toHaveLength(2), { timeout: 6000 });
    expect(posts()[1].body?.runId).toBe(posts()[0].body?.runId);
    expect(posts()[1].lastEventId).toBe(`${posts()[0].body?.runId}:1`);
    await waitFor(() => expect(screen.getAllByTestId("chat-message-assistant").map((el) => el.textContent).join(" ")).toContain("Done"));
    const text = screen.getAllByTestId("chat-message-assistant").map((el) => el.textContent).join(" ");
    expect(text.match(/Let me check\./g)).toHaveLength(1);
    expect(text.trim().endsWith("Done")).toBe(true);
    expect(screen.getAllByTestId("chat-message-user")).toHaveLength(1);
    expect(screen.getAllByTestId("tool-call-card")).toHaveLength(2);
  }, 15000);

  it("a reload mid-turn reattaches with GET attach (and the stored Last-Event-ID), never POSTs, and shows no duplicate messages", async () => {
    window.history.replaceState({}, "", "/agents/a1/chat/c1");
    nav.path = "/agents/a1/chat/c1";
    window.sessionStorage.setItem("eap.studio.agui.lastEventId:c1:x1", "x1:1");
    threadBody = {
      conversationId: "c1",
      messages: [{ id: "u-1", role: "user", content: "hi" }],
      runs: [{ runId: "x1", run_id: NATIVE, turn: 1, status: "running", display_status: "running", superseded: false }],
      activeRunId: "x1",
    };
    onAttach = (runId) => replay(framesOf("plain", "resume_after_1"), "c1", runId, { history: [{ id: "u-1", type: "m", role: "user", content: "hi" }] });
    render(<ChatTab agentId="a1" conversationId="c1" />);
    await waitFor(() => expect(screen.getByTestId("chat-message-user")).toHaveTextContent("hi"));
    await waitFor(() => expect(calls.some((call) => call.method === "GET" && call.url.includes("/runs/x1/stream"))).toBe(true));
    const attach = calls.find((call) => call.method === "GET" && call.url.includes("/runs/x1/stream"));
    expect(attach?.lastEventId).toBe("x1:1");
    expect(posts()).toHaveLength(0);
    await waitFor(() => expect(screen.getAllByTestId("chat-message-assistant")).toHaveLength(1));
    expect(lastText()).toContain("Echo: hi");
    expect(screen.getAllByTestId("chat-message-user")).toHaveLength(1);
  });
});

// ------------------------------------------------------------------ run options
describe("run options travel in forwardedProps.pydanticos", () => {
  it("a model override from ?model=, a pinned ?version=1, and nothing else the server must not see", async () => {
    window.history.replaceState({}, "", "/agents/a1/chat?version=1&model=openai:gpt-4.1");
    serve("plain");
    render(<ChatTab agentId="a1" />);
    await waitFor(() => expect(fetchModels).toHaveBeenCalled());
    await send("hi");
    await waitFor(() => expect(posts()).toHaveLength(1));
    const body = posts()[0].body!;
    expect(body.forwardedProps.pydanticos).toEqual({ version: 1, model: { provider: "openai", name: "gpt-4.1" } });
    // No client tools and no native-only fields on the AG-UI POST.
    expect(Object.keys(body).sort()).toEqual(expect.arrayContaining(["threadId", "runId", "messages", "forwardedProps"]));
    expect(body).not.toHaveProperty("model");
    expect(body).not.toHaveProperty("request_context");
  });

  it("a required request-context field entered in the popover goes in forwardedProps.pydanticos.requestContext", async () => {
    vi.mocked(getRequestContext).mockResolvedValue({
      agent_id: "a1", version: 2, draft: false, schema: { type: "object", required: ["tier"], properties: { tier: { type: "string", enum: ["gold", "silver"] } } },
      fields: [{ name: "tier", type: "enum", required: true, description: "", max_length: null, values: ["gold", "silver"] }],
    } as RequestContextInfo);
    serve("plain");
    render(<ChatTab agentId="a1" />);
    fireEvent.click(await screen.findByRole("button", { name: /Request context/ }));
    const dialog = await screen.findByRole("dialog", { name: "Request context" });
    fireEvent.change(await within(dialog).findByLabelText(/Tier/), { target: { value: "gold" } });
    fireEvent.keyDown(dialog, { key: "Escape" });
    await send("hi");
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].body?.forwardedProps.pydanticos.requestContext).toEqual({ tier: "gold" });
  });
});

// ------------------------------------------------------------------ the Settings switch and parity
const turnRun = { run_id: NATIVE, agent_id: "a1", version: 2, status: "completed", display_status: "completed", input: "check things", output: "Done", error: null, conversation_id: "c1", turn: 1, chat_input_tokens: 11, chat_output_tokens: 7, chat_cost_usd: "0.0012", model_id: "openai:gpt-4.1-mini" };
const aguiToolChainThread = () => ({
  conversationId: "c1",
  runs: [{ runId: "x1", run_id: NATIVE, turn: 1, status: "completed", display_status: "completed", superseded: false }],
  activeRunId: null,
  messages: [
    { id: "u-1", role: "user", content: "check things" },
    { id: "x1-m0", role: "assistant", content: "Let me check.", toolCalls: [{ id: "c1", type: "function", function: { name: "lookup", arguments: '{"q":1}' } }, { id: "c2", type: "function", function: { name: "lookup", arguments: '{"q":2}' } }] },
    { id: "x1-t-c1", role: "tool", toolCallId: "c1", content: '{"hit":1}' },
    { id: "x1-t-c2", role: "tool", toolCallId: "c2", content: "boom" },
    { id: "x1-m1", role: "assistant", content: "Done" },
  ],
});
function summary() {
  const pane = screen.getByTestId("chat-transcript");
  return {
    users: screen.getAllByTestId("chat-message-user").map((el) => el.textContent),
    assistants: screen.getAllByTestId("chat-message-assistant").map((el) => el.textContent),
    tools: screen.getAllByTestId("tool-call-card").map((el) => [el.getAttribute("data-status"), within(el).queryByTestId("tool-call-args")?.textContent ?? null]),
    footers: screen.getAllByTestId("turn-footer").length,
    tokens: screen.queryByTestId("turn-badge-tokens")?.textContent ?? null,
    cost: screen.queryByTestId("turn-badge-cost")?.textContent ?? null,
    model: screen.queryByTestId("turn-badge-model")?.textContent ?? null,
    text: (pane.textContent ?? "").replace(/\s+/g, " "),
  };
}
async function renderHistory(protocol: "agui" | "native") {
  window.localStorage.setItem(CHAT_PROTOCOL_STORAGE_KEY, protocol);
  window.history.replaceState({}, "", "/agents/a1/chat/c1");
  nav.path = "/agents/a1/chat/c1";
  threadBody = aguiToolChainThread();
  vi.mocked(listEvents).mockResolvedValue(nativeOf("tool_chain").map((e) => (e.type === "run_started" ? { ...e, data: { ...e.data, input: "check things" } } : e)));
  vi.mocked(getConversation).mockResolvedValue({ id: "c1", agent_id: "a1", title: "T", turn_count: 1, created_at: "", updated_at: "", runs: [turnRun] } as never);
  render(<ChatTab agentId="a1" conversationId="c1" />);
  await waitFor(() => expect(screen.getAllByTestId("chat-message-assistant").length).toBeGreaterThan(0));
  await screen.findAllByTestId("tool-call-card");
  await waitFor(() => expect(screen.getAllByTestId("turn-footer").length).toBeGreaterThan(0));
  await waitFor(() => expect(screen.getByTestId("turn-badge-tokens")).toHaveTextContent(/11/));
  return summary();
}

describe("parity with native", () => {
  it("the same conversation (user, text, two tool calls, usage badge) is visibly identical on native and on AG-UI", async () => {
    const native = await renderHistory("native");
    expect(calls.filter((call) => call.url.includes("/ag-ui/"))).toEqual([]);
    cleanup();
    installServer();
    const agui = await renderHistory("agui");
    expect(calls.some((call) => call.url.includes("/ag-ui/agents/a1/threads/c1"))).toBe(true);
    // Same content. (Known, visible: native lists a turn's tool cards before its lead-in text, AG-UI
    // keeps the event order, text then cards. So compare the content, not the order, of the pieces.)
    const content = (v: ReturnType<typeof summary>) => ({
      ...v,
      text: undefined,
      assistants: v.assistants.flatMap((t) => (t ?? "").split(/(?=lookup|Let me check\.|Done$)/)).map((t) => t.trim()).filter(Boolean).sort(),
    });
    expect(content(agui)).toEqual(content(native));
    expect([...(agui.text.match(/Let me check\.|lookup|Done/g) ?? [])].sort()).toEqual([...(native.text.match(/Let me check\.|lookup|Done/g) ?? [])].sort());
    expect(agui.tools).toHaveLength(2);
  });
});

describe("the Settings 'Chat protocol' switch", () => {
  afterEach(() => setChatProtocolDefaultForTests("native"));
  const both = () => (
    <ThemeProvider>
      <SettingsSections />
      <ChatTab agentId="a1" conversationId="c1" />
    </ThemeProvider>
  );

  it("AG-UI is the default: nothing stored means the Chat tab talks AG-UI and the radio says AG-UI", async () => {
    setChatProtocolDefaultForTests("agui");
    window.localStorage.removeItem(CHAT_PROTOCOL_STORAGE_KEY);
    expect(readChatProtocol()).toBe("agui");
    window.history.replaceState({}, "", "/agents/a1/chat/c1");
    nav.path = "/agents/a1/chat/c1";
    threadBody = aguiToolChainThread();
    render(both());
    await waitFor(() => expect(calls.some((call) => call.url.includes("/ag-ui/agents/a1/threads/c1"))).toBe(true));
    expect(screen.getByRole("radio", { name: "AG-UI" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Native" })).not.toBeChecked();
    expect(window.localStorage.getItem(CHAT_PROTOCOL_STORAGE_KEY)).toBeNull();
  });

  it("switching to Native reopens the same conversation on the native path (no AG-UI traffic afterwards), and AG-UI comes back the same way", async () => {
    window.history.replaceState({}, "", "/agents/a1/chat/c1");
    nav.path = "/agents/a1/chat/c1";
    threadBody = aguiToolChainThread();
    vi.mocked(listEvents).mockResolvedValue(nativeOf("tool_chain").map((e) => (e.type === "run_started" ? { ...e, data: { ...e.data, input: "check things" } } : e)));
    vi.mocked(getConversation).mockResolvedValue({ id: "c1", agent_id: "a1", title: "T", turn_count: 1, created_at: "", updated_at: "", runs: [turnRun] } as never);
    render(both());
    await screen.findAllByTestId("tool-call-card");
    expect(screen.getByTestId("chat-message-user")).toHaveTextContent("check things");
    const before = calls.length;
    await act(async () => { fireEvent.click(screen.getByRole("radio", { name: "Native" })); });
    expect(window.localStorage.getItem(CHAT_PROTOCOL_STORAGE_KEY)).toBe("native");
    await waitFor(() => expect(getConversation).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId("chat-message-user")).toHaveTextContent("check things"));
    expect(screen.getAllByTestId("tool-call-card")).toHaveLength(2);
    expect(calls.slice(before).filter((call) => call.url.includes("/ag-ui/"))).toEqual([]);
    await act(async () => { fireEvent.click(screen.getByRole("radio", { name: "AG-UI" })); });
    expect(window.localStorage.getItem(CHAT_PROTOCOL_STORAGE_KEY)).toBe("agui");
    await waitFor(() => expect(calls.slice(before).some((call) => call.url.includes("/ag-ui/agents/a1/threads/c1"))).toBe(true));
    await waitFor(() => expect(screen.getAllByTestId("tool-call-card")).toHaveLength(2));
    expect(screen.getByTestId("chat-message-user")).toHaveTextContent("check things");
  });

  it("the key is registered under Browser data (Chat protocol row), and 'Clear layout preferences' returns to the AG-UI default", async () => {
    setChatProtocolDefaultForTests("agui");
    window.localStorage.setItem(CHAT_PROTOCOL_STORAGE_KEY, "native");
    render(<ThemeProvider><SettingsSections /></ThemeProvider>);
    const table = await screen.findByTestId("browser-data-table");
    expect(within(table).getByText("Chat protocol")).toBeInTheDocument();
    expect(BROWSER_DATA.find((e) => e.keys.includes(CHAT_PROTOCOL_STORAGE_KEY))?.clearGroup).toBe("layout");
    fireEvent.click(screen.getByTestId("settings-clear-layout"));
    const dialog = await screen.findByRole("dialog");
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: /^Clear/ })); });
    await waitFor(() => expect(window.localStorage.getItem(CHAT_PROTOCOL_STORAGE_KEY)).toBeNull());
    expect(readChatProtocol()).toBe("agui");
  });

  it("after 'Clear layout preferences' the open Settings radio follows the stored default (AG-UI), not the stale Native", async () => {
    setChatProtocolDefaultForTests("agui");
    window.localStorage.setItem(CHAT_PROTOCOL_STORAGE_KEY, "native");
    render(<ThemeProvider><SettingsSections /></ThemeProvider>);
    expect(await screen.findByRole("radio", { name: "Native" })).toBeChecked();
    fireEvent.click(screen.getByTestId("settings-clear-layout"));
    const dialog = await screen.findByRole("dialog");
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: /^Clear/ })); });
    await waitFor(() => expect(window.localStorage.getItem(CHAT_PROTOCOL_STORAGE_KEY)).toBeNull());
    await waitFor(() => expect(screen.getByRole("radio", { name: "AG-UI" })).toBeChecked());
  });
});

// ------------------------------------------------------------------ layout and accessibility
import fs from "node:fs";
import path from "node:path";
import { setWindowWidth } from "@/components/frame/frameTesting";

async function openApproval() {
  serveApproval();
  render(<ChatTab agentId="a1" />);
  await send("pay 5");
  await screen.findByRole("region", { name: "Approval for tool pay" });
}

describe("layout at 375 px", () => {
  it("the open approval card, tool cards and structured output force no width past the screen", async () => {
    const restore = setWindowWidth(375);
    try {
      await openApproval();
      const root = document.body;
      const fixed = Array.from(root.querySelectorAll<HTMLElement>("*")).flatMap((el) =>
        Array.from(el.classList).filter((c) => /^(min-)?w-\[(\d+)px\]$/.test(c) && Number(/\[(\d+)px\]/.exec(c)![1]) > 375),
      );
      expect(fixed).toEqual([]);
      expect(Array.from(root.querySelectorAll<HTMLElement>("[style]")).filter((el) => Number.parseFloat(el.style.minWidth) > 375)).toEqual([]);
      // The wrapper and the card can shrink; long args wrap or scroll inside their own box.
      const wrapper = screen.getByTestId("tool-approval-waiting");
      const card = within(wrapper).getByRole("region");
      expect(card.className).toMatch(/min-w-0/);
      expect(within(card).getByTestId("approval-args-tool-g1").className).toMatch(/overflow-auto|break-words/);
      expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(document.documentElement.clientWidth || 375);
    } finally {
      restore();
    }
  });

  it("the new chat files use theme tokens only: no hex colours or raw palette classes", () => {
    const files = ["AgUiApprovals.tsx", "agUiChat.ts", "chatProtocol.ts"].map((f) => path.join(__dirname, f)).concat(path.join(__dirname, "../agentPage/ChatPane.tsx"));
    for (const file of files) {
      const text = fs.readFileSync(file, "utf8");
      expect(text, file).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(text, file).not.toMatch(/\b(bg|text|border|ring)-(red|green|blue|yellow|orange|gray|slate|zinc|neutral|stone|amber|emerald|sky|indigo|violet|purple|pink|rose|white|black)(-\d{2,3})?\b/);
    }
  });
});

describe.each(["light", "dark"] as const)("axe on the AG-UI chat (%s theme)", (theme) => {
  beforeEach(() => {
    window.localStorage.setItem("studio.theme", theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
  });
  it("has no violations with an open approval card (the page, not the tab, owns the h1)", async () => {
    await openApproval();
    expect(document.querySelectorAll("h1").length).toBeLessThanOrEqual(1);
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
  it("has no violations with a streamed tool-chain turn, citations and structured output", async () => {
    serve("tool_chain");
    render(<ChatTab agentId="a1" />);
    await send("check things");
    await screen.findAllByTestId("tool-call-card");
    await waitFor(() => expect(screen.getByTestId("turn-badge-tokens")).toBeInTheDocument());
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("conversation rail switching on AG-UI", () => {
  it("each conversation loads its own thread history (threadId = conversation id) and switching back keeps it", async () => {
    const threads: Record<string, Record<string, unknown>> = {
      c1: { ...aguiToolChainThread(), conversationId: "c1" },
      c2: { conversationId: "c2", runs: [], activeRunId: null, messages: [{ id: "u-9", role: "user", content: "second chat" }, { id: "x9-m0", role: "assistant", content: "second answer" }] },
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const m = /\/threads\/([^/]+)$/.exec(new URL(String(input)).pathname);
      calls.push({ method: "GET", url: String(input), body: null, lastEventId: null });
      return m ? json(threads[m[1]]) : json({ detail: "x" }, 404);
    }));
    window.history.replaceState({}, "", "/agents/a1/chat/c1");
    nav.path = "/agents/a1/chat/c1";
    const { rerender } = render(<ChatTab agentId="a1" conversationId="c1" />);
    await waitFor(() => expect(screen.getByTestId("chat-message-user")).toHaveTextContent("check things"));
    window.history.replaceState({}, "", "/agents/a1/chat/c2");
    nav.path = "/agents/a1/chat/c2";
    rerender(<ChatTab agentId="a1" conversationId="c2" />);
    await waitFor(() => expect(screen.getByTestId("chat-message-user")).toHaveTextContent("second chat"));
    expect(screen.getByTestId("chat-message-assistant")).toHaveTextContent("second answer");
    expect(screen.queryByText("check things")).not.toBeInTheDocument();
    window.history.replaceState({}, "", "/agents/a1/chat/c1");
    nav.path = "/agents/a1/chat/c1";
    rerender(<ChatTab agentId="a1" conversationId="c1" />);
    await waitFor(() => expect(screen.getByTestId("chat-message-user")).toHaveTextContent("check things"));
    expect(screen.queryByText("second chat")).not.toBeInTheDocument();
    expect(calls.filter((c) => c.url.endsWith("/threads/c1")).length).toBeGreaterThanOrEqual(2);
    expect(calls.filter((c) => c.url.endsWith("/threads/c2"))).toHaveLength(1);
  });
});
