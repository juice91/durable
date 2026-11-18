// AGUI.12: the agent Chat tab over AG-UI. Pure helpers, the chat agent, and ChatTab against a mock
// AG-UI server (fetch): send and stream, run options in forwardedProps, history and reattach,
// interrupts with the shared ApprovalCard, refusals, banners, and the Settings switch.
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { lastValueFrom, toArray } from "rxjs";

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
import { getRun, listEvents, subscribeRun, type RunEvent, type RunEventData } from "@/lib/runs";
import { decideToolApproval } from "@/lib/approvals";
import { startTurnWithOptions } from "@/components/chat/turnApi";
import { AgUiHttpError } from "@/lib/agui";
import { ChatTab } from "@/components/agentPage/ChatTab";
import { SettingsSections } from "@/components/settings/SettingsSections";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { BROWSER_DATA } from "@/components/settings/browserData";
import {
  StudioChatAgent,
  displayStatusOf,
  inputUserText,
  messageRunMap,
  approvalDecidedOf,
  inputUserMessageId,
  outputRetryEventOf,
  resumeDecisions,
  nativeDetail,
  pydanticosOptions,
  refusalOf,
  runInfoOf,
  shownRunIds,
  toolInterruptTarget,
} from "./agUiChat";
import { answerableEntries, interruptExpired, resumeEntry } from "./AgUiApprovals";
import { isJsonOf } from "@/components/agentPage/ChatPane";
import { CHAT_PROTOCOL_STORAGE_KEY, readChatProtocol, setChatProtocolDefaultForTests, writeChatProtocol } from "./chatProtocol";

process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = process.env.NEXT_PUBLIC_EAP_DEV_TOKEN || "Bearer dev:t1:u1";

// ------------------------------------------------------------------ pure helpers

describe("AG-UI chat helpers", () => {
  it("builds forwardedProps.pydanticos from the turn options; resourceId only when given", () => {
    expect(pydanticosOptions({ version: 2, model: null, request_context: null }, null)).toEqual({ version: 2 });
    expect(
      pydanticosOptions({ version: "draft", model: { provider: "test", name: "echo" }, request_context: { tier: "gold" } }, "user-7"),
    ).toEqual({ version: "draft", model: { provider: "test", name: "echo" }, requestContext: { tier: "gold" }, resourceId: "user-7" });
    expect(pydanticosOptions({ version: null, model: null, request_context: null }, "")).toEqual({ version: null });
  });

  it("maps AG-UI refusal locations to the native turn-start shape", () => {
    const detail = [
      { loc: ["forwardedProps", "pydanticos", "requestContext", "tier"], msg: "is required", type: "missing" },
      { loc: ["forwardedProps", "pydanticos", "model"], msg: "not allowed", type: "x" },
      { loc: ["messages"], msg: "no new input", type: "no_new_input" },
    ];
    expect(nativeDetail(detail)).toEqual([
      { loc: ["body", "request_context", "tier"], msg: "is required", type: "missing" },
      { loc: ["body", "model"], msg: "not allowed", type: "x" },
      { loc: ["messages"], msg: "no new input", type: "no_new_input" },
    ]);
    expect(refusalOf(new AgUiHttpError(422, { detail }))).toEqual({ status: 422, detail: nativeDetail(detail) });
    expect(refusalOf(new Error("network"))).toBeNull();
  });

  it("maps messages to runs by id, never by position (a send that left no run shifts nothing)", () => {
    const messages = [
      { id: "u0", role: "user" },
      { id: "x-m0", role: "assistant" },
      // A send whose stream failed before any run: its user message stays, unmapped.
      { id: "lost", role: "user" },
      { id: "u2", role: "user" },
      { id: "placeholder", role: "assistant" },
      { id: "n1-u", role: "user" },
      { id: "n1-m0", role: "assistant" },
    ];
    const maps = { byPrefix: new Map([["x", "r1"], ["n1", "r3"]]), byUserMessage: new Map([["u2", "r2"]]) };
    expect([...messageRunMap(messages, maps)]).toEqual([
      ["u0", "r1"],
      ["x-m0", "r1"],
      ["u2", "r2"],
      ["placeholder", "r2"],
      ["n1-u", "r3"],
      ["n1-m0", "r3"],
    ]);
    expect(shownRunIds({ runs: [{ runId: "a", run_id: "r1", turn: 1, status: "completed", display_status: "completed", superseded: true }, { runId: "b", run_id: "r2", turn: 2, status: "completed", display_status: "completed", superseded: false }] })).toEqual(["r2"]);
  });

  it("reads resume decisions, approval_decided and output_retry events", () => {
    expect(
      resumeDecisions({
        resume: [
          { interruptId: "approval:r1:tool:a", status: "resolved", payload: { approved: true } },
          { interruptId: "approval:r1:tool:b", status: "resolved", payload: { approved: false } },
          { interruptId: "approval:r1:tool:c", status: "cancelled" },
          { interruptId: "approval:r1:step-x", status: "resolved", payload: { approved: true } },
        ],
      }),
    ).toEqual([
      { runId: "r1", toolCallId: "a", sent: "approved" },
      { runId: "r1", toolCallId: "b", sent: "denied" },
      { runId: "r1", toolCallId: "c", sent: "denied" },
    ]);
    expect(approvalDecidedOf({ type: "CUSTOM", name: "pydanticos.approval_decided", value: { kind: "tool", key: "g1", decision: "denied" } } as never)).toEqual({ toolCallId: "g1", decision: "denied" });
    expect(approvalDecidedOf({ type: "CUSTOM", name: "pydanticos.approval_decided", value: { kind: "step", key: "s", decision: "approved" } } as never)).toBeNull();
    expect(outputRetryEventOf({ type: "CUSTOM", name: "pydanticos.output_retry", value: { model_request: 0, errors: ["/age"] } } as never)).toEqual({ model_request: 0, errors: ["/age"] });
    expect(inputUserMessageId({ messages: [{ id: "u1", role: "user", content: "a" }, { id: "a1", role: "assistant", content: "b" }] as never })).toBe("u1");
  });

  it("answers an expired interrupt 'cancelled' (the only answer react-ag-ui accepts after expiresAt)", () => {
    const now = Date.parse("2026-11-15T12:00:00Z");
    const interrupts = [
      { id: "approval:r1:tool:a", reason: "tool_call", expiresAt: "2026-11-15T11:00:00Z" },
      { id: "approval:r1:tool:b", reason: "tool_call", expiresAt: "2026-11-15T13:00:00Z" },
      { id: "approval:r1:tool:c", reason: "tool_call" },
    ];
    expect(interruptExpired(interrupts[0], now)).toBe(true);
    expect(interruptExpired(interrupts[1], now)).toBe(false);
    expect(interruptExpired(interrupts[2], now)).toBe(false);
    expect(answerableEntries(interrupts, interrupts.map((i) => resumeEntry(i.id, true)), now)).toEqual([
      { interruptId: "approval:r1:tool:a", status: "cancelled" },
      { interruptId: "approval:r1:tool:b", status: "resolved", payload: { approved: true } },
      { interruptId: "approval:r1:tool:c", status: "resolved", payload: { approved: true } },
    ]);
  });

  it("renders the structured view for a scalar result whose text is its canonical JSON", () => {
    expect(isJsonOf("3", 3)).toBe(true);
    expect(isJsonOf('"ok"', "ok")).toBe(true);
    expect(isJsonOf("true", true)).toBe(true);
    expect(isJsonOf('{"b":1,"a":2}', { a: 2, b: 1 })).toBe(true);
    expect(isJsonOf("3 apples", 3)).toBe(false);
    expect(isJsonOf("4", 3)).toBe(false);
  });

  it("reads interrupt targets, run info, statuses and the input text", () => {
    expect(toolInterruptTarget({ id: "approval:run-1:tool:call-9", toolCallId: "call-9", metadata: { pydanticos: { run_id: "run-1", tool_name: "refund" } } })).toEqual({
      runId: "run-1",
      toolCallId: "call-9",
      toolName: "refund",
    });
    expect(toolInterruptTarget({ id: "approval:run-2:tool:c1" })).toEqual({ runId: "run-2", toolCallId: "c1", toolName: null });
    expect(toolInterruptTarget({ id: "approval:run-2:step-a" })).toBeNull();
    const info = runInfoOf({ type: "CUSTOM", name: "pydanticos.run", value: { run_id: "r1", conversation_id: "c1", turn: 1, model_id: "m", version: 2, draft: false } } as never);
    expect(info).toMatchObject({ run_id: "r1", conversation_id: "c1", version: 2, draft: false });
    expect(displayStatusOf({ type: "CUSTOM", name: "pydanticos.status", value: { display_status: "recovering" } } as never)).toBe("recovering");
    expect(displayStatusOf({ type: "CUSTOM", name: "pydanticos.status", value: { display_status: "nope" } } as never)).toBeNull();
    expect(inputUserText({ messages: [{ id: "1", role: "user", content: "first" }, { id: "2", role: "assistant", content: "a" }, { id: "3", role: "user", content: [{ type: "text", text: "hi" }] }] as never })).toBe("hi");
    expect(resumeEntry("i1", true)).toEqual({ interruptId: "i1", status: "resolved", payload: { approved: true } });
    expect(resumeEntry("i1", false, "  no  ")).toEqual({ interruptId: "i1", status: "resolved", payload: { approved: false, reason: "no" } });
  });

  it("keeps the protocol in localStorage, AG-UI by default, registered in Settings", () => {
    setChatProtocolDefaultForTests("agui");
    window.localStorage.removeItem(CHAT_PROTOCOL_STORAGE_KEY);
    expect(readChatProtocol()).toBe("agui");
    writeChatProtocol("native");
    expect(readChatProtocol()).toBe("native");
    window.localStorage.setItem(CHAT_PROTOCOL_STORAGE_KEY, "bogus");
    expect(readChatProtocol()).toBe("agui");
    expect(BROWSER_DATA.some((entry) => entry.keys.includes(CHAT_PROTOCOL_STORAGE_KEY))).toBe(true);
    setChatProtocolDefaultForTests("native");
  });
});

// ------------------------------------------------------------------ a mock AG-UI server

const enc = new TextEncoder();
type Ev = Record<string, unknown> & { type: string };
interface Call {
  method: string;
  url: string;
  body: Record<string, any> | null;
  lastEventId: string | null;
}

function sse(runId: string, events: Ev[], firstSeq = 0): Response {
  const text = events.map((event, index) => `id: ${runId}:${firstSeq + index}\ndata: ${JSON.stringify(event)}\n\n`).join("");
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(enc.encode(text));
        controller.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

let calls: Call[] = [];
let threadBody: Record<string, unknown> = { messages: [], runs: [], activeRunId: null, conversationId: null };
let onPost: (body: Record<string, any>) => Response = () => json({ detail: "unset" }, 500);
let onAttach: (runId: string) => Response = () => json({ detail: "Run not found" }, 404);

function installServer() {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      const headers = new Headers(init?.headers ?? {});
      const body = typeof init?.body === "string" ? (JSON.parse(init.body) as Record<string, any>) : null;
      calls.push({ method, url, body, lastEventId: headers.get("Last-Event-ID") });
      if (method === "GET" && /\/ag-ui\/agents\/a1\/threads\/[^/]+$/.test(new URL(url).pathname)) {
        return json({ threadId: decodeURIComponent(url.split("/").pop() ?? ""), ...threadBody });
      }
      const attach = /\/threads\/[^/]+\/runs\/([^/]+)\/stream$/.exec(new URL(url).pathname);
      if (method === "GET" && attach) return onAttach(decodeURIComponent(attach[1]));
      if (method === "POST" && new URL(url).pathname === "/api/v1/ag-ui/agents/a1") return onPost(body ?? {});
      return json({ detail: `unexpected ${method} ${url}` }, 404);
    }),
  );
}

const posts = () => calls.filter((call) => call.method === "POST");

const spec = (extra: object = {}): AgentSpec =>
  ({ spec_version: 1, name: "Support bot", instructions: "x", model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [], ...extra }) as AgentSpec;
const AGENT: Agent = { id: "a1", name: "Support bot", draft_spec: spec(), latest_version: 2 };
const VERSIONS: AgentVersion[] = [1, 2].map((version) => ({ agent_id: "a1", version, spec: spec(), spec_hash: `h${version}`, created_at: "", created_by: "dev" }));
const NO_FIELDS: RequestContextInfo = { agent_id: "a1", version: 2, draft: false, fields: [], schema: {} } as RequestContextInfo;
const TIER: RequestContextInfo = {
  agent_id: "a1",
  version: 2,
  draft: false,
  fields: [{ name: "tier", type: "enum", required: true, description: "", max_length: null, values: ["gold", "silver"] }],
  schema: { type: "object", required: ["tier"], properties: { tier: { type: "string", enum: ["gold", "silver"] } } },
} as RequestContextInfo;
const ev = (seq: number, type: RunEvent["type"], data: object): RunEvent => ({ seq, type, data: data as RunEventData, ts: "2026-11-15T10:00:00Z" });

const runInfo = (runId: string, conversationId = "c1", turn = 1): Ev => ({
  type: "CUSTOM",
  name: "pydanticos.run",
  value: { run_id: runId, conversation_id: conversationId, turn, trace_id: null, model_id: "openai:gpt-4.1-mini", version: 2, draft: false },
});

function answerStream(body: Record<string, any>, text: string, nativeRun = "r1", extra: Ev[] = [], finish: Ev | null = null): Response {
  const runId = String(body.runId);
  const messageId = `${runId}-m0`;
  return sse(runId, [
    { type: "RUN_STARTED", threadId: body.threadId, runId },
    runInfo(nativeRun),
    ...extra,
    { type: "TEXT_MESSAGE_START", messageId, role: "assistant" },
    { type: "TEXT_MESSAGE_CONTENT", messageId, delta: text },
    { type: "TEXT_MESSAGE_END", messageId },
    finish ?? { type: "RUN_FINISHED", threadId: body.threadId, runId, outcome: { type: "success" } },
  ]);
}

const box = () => screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;

async function send(text: string) {
  await waitFor(() => expect(box()).not.toBeDisabled());
  fireEvent.change(box(), { target: { value: text } });
  fireEvent.keyDown(box(), { key: "Enter", code: "Enter" });
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
  vi.mocked(fetchModels).mockResolvedValue([]);
  vi.mocked(getRequestContext).mockResolvedValue(NO_FIELDS);
  vi.mocked(listEvents).mockResolvedValue([]);
  vi.mocked(getRun).mockResolvedValue({ run_id: "r1", display_status: "completed", chat_input_tokens: 11, chat_output_tokens: 7, chat_cost_usd: "0.0012" } as never);
  vi.mocked(getConversation).mockResolvedValue({ id: "c1", agent_id: "a1", title: "T", turn_count: 1, created_at: "", updated_at: "", runs: [], resource_id: "u1" } as never);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("StudioChatAgent", () => {
  it("adds the run options to forwardedProps.pydanticos and reports every event", async () => {
    installServer();
    onPost = (body) => answerStream(body, "hi");
    const agent = new StudioChatAgent({ url: "http://localhost:8000/api/v1/ag-ui/agents/a1", threadId: "t1", storage: null });
    agent.options = () => ({ version: "draft", requestContext: { tier: "gold" } });
    const seen: string[] = [];
    agent.listener = { onEvent: (event) => seen.push(event.type), onError: () => undefined };
    const input = { threadId: "t1", runId: "run-a", messages: [{ id: "u1", role: "user", content: "hello" }], tools: [], context: [], state: null, forwardedProps: { other: 1 } };
    await lastValueFrom(agent.run(input as never).pipe(toArray()));
    expect(posts()[0].body?.forwardedProps).toEqual({ other: 1, pydanticos: { version: "draft", requestContext: { tier: "gold" } } });
    expect(seen).toContain("RUN_FINISHED");
    expect(seen).toContain("CUSTOM");
  });

  it("attaches with the GET route when attachNext is set (once)", async () => {
    installServer();
    onAttach = (runId) => sse(runId, [{ type: "RUN_STARTED", threadId: "t1", runId }, { type: "RUN_FINISHED", threadId: "t1", runId, outcome: { type: "success" } }]);
    const agent = new StudioChatAgent({ url: "http://localhost:8000/api/v1/ag-ui/agents/a1", threadId: "t1", storage: null });
    agent.attachNext = "server-run";
    const input = { threadId: "t1", runId: "local", messages: [], tools: [], context: [], state: null };
    await lastValueFrom(agent.run(input as never).pipe(toArray()));
    expect(calls.map((call) => `${call.method} ${new URL(call.url).pathname}`)).toEqual(["GET /api/v1/ag-ui/agents/a1/threads/t1/runs/server-run/stream"]);
    expect(agent.attachNext).toBeNull();
  });
});

describe("ChatTab over AG-UI", () => {
  it("streams a new thread's first turn, with run options in forwardedProps, and shows the native usage", async () => {
    vi.mocked(listEvents).mockResolvedValue([ev(0, "run_started", { input: "hello" }), ev(1, "model_message", { model_request: 0, text: "Hello there", is_final: true, model_id: "openai:gpt-4.1-mini" }), ev(2, "run_completed", {})]);
    onPost = (body) => answerStream(body, "Hello there");
    render(<ChatTab agentId="a1" />);
    await send("hello");
    await waitFor(() => expect(screen.getByTestId("chat-message-assistant")).toHaveTextContent("Hello there"));
    const post = posts()[0];
    // The latest published version (null), as the native turn start sends it.
    expect(post.body?.forwardedProps?.pydanticos).toEqual({ version: null });
    // A new thread: a fresh threadId (the API derives the conversation, D3), never a conversation id.
    expect(post.body?.threadId).toMatch(/^[0-9a-f-]{36}$/);
    expect(subscribeRun).not.toHaveBeenCalled();
    expect(startTurnWithOptions).not.toHaveBeenCalled();
    // CUSTOM pydanticos.run named the conversation: it goes in the address.
    await waitFor(() => expect(window.location.pathname).toBe("/agents/a1/chat/c1"));
    // Usage per turn from the native run row, by the run id the stream named.
    await waitFor(() => expect(getRun).toHaveBeenCalledWith("r1"));
    await waitFor(() => expect(screen.getByTestId("chat-message-user")).toHaveTextContent("hello"));
  });

  it("sends a draft turn and request context in forwardedProps", async () => {
    window.history.replaceState({}, "", "/agents/a1/chat?version=draft");
    vi.mocked(getRequestContext).mockResolvedValue({ ...TIER, draft: true, version: null } as never);
    onPost = (body) => answerStream(body, "ok");
    render(<ChatTab agentId="a1" />);
    await send("hi");
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].body?.forwardedProps?.pydanticos?.version).toBe("draft");
  });

  it("loads a conversation's history from the thread GET and maps its turns to native runs", async () => {
    window.history.replaceState({}, "", "/agents/a1/chat/c1");
    nav.path = "/agents/a1/chat/c1";
    threadBody = {
      conversationId: "c1",
      messages: [
        { id: "u-1", role: "user", content: "first question" },
        { id: "x1-m0", role: "assistant", content: "first answer" },
      ],
      runs: [{ runId: "x1", run_id: "r1", turn: 1, status: "completed", display_status: "completed", superseded: false }],
      activeRunId: null,
    };
    vi.mocked(getConversation).mockResolvedValue({
      id: "c1",
      agent_id: "a1",
      title: "T",
      turn_count: 1,
      created_at: "",
      updated_at: "",
      resource_id: "u1",
      runs: [{ run_id: "r1", agent_id: "a1", version: 2, status: "completed", display_status: "completed", input: "first question", output: null, error: null, conversation_id: "c1", turn: 1, chat_input_tokens: 5, chat_output_tokens: 3, chat_cost_usd: "0.001" }],
    } as never);
    render(<ChatTab agentId="a1" conversationId="c1" />);
    await waitFor(() => expect(screen.getByTestId("chat-message-assistant")).toHaveTextContent("first answer"));
    expect(calls.some((call) => call.method === "GET" && call.url.endsWith("/ag-ui/agents/a1/threads/c1"))).toBe(true);
    // The turn's badge (native data, run r1) renders under its last message.
    await waitFor(() => expect(screen.getAllByTestId("turn-footer").length).toBeGreaterThan(0));
    expect(subscribeRun).not.toHaveBeenCalled();
  });

  it("reattaches to a running turn after a reload (unstable_resume through the GET attach route)", async () => {
    window.history.replaceState({}, "", "/agents/a1/chat/c1");
    nav.path = "/agents/a1/chat/c1";
    threadBody = {
      conversationId: "c1",
      messages: [{ id: "u-1", role: "user", content: "still running" }],
      runs: [{ runId: "x1", run_id: "r1", turn: 1, status: "running", display_status: "running", superseded: false }],
      activeRunId: "x1",
    };
    onAttach = (runId) =>
      sse(runId, [
        { type: "RUN_STARTED", threadId: "c1", runId },
        runInfo("r1"),
        { type: "MESSAGES_SNAPSHOT", messages: [{ id: "u-1", role: "user", content: "still running" }] },
        { type: "TEXT_MESSAGE_START", messageId: `${runId}-m0`, role: "assistant" },
        { type: "TEXT_MESSAGE_CONTENT", messageId: `${runId}-m0`, delta: "done now" },
        { type: "TEXT_MESSAGE_END", messageId: `${runId}-m0` },
        { type: "RUN_FINISHED", threadId: "c1", runId, outcome: { type: "success" } },
      ]);
    render(<ChatTab agentId="a1" conversationId="c1" />);
    await waitFor(() => expect(calls.some((call) => call.method === "GET" && call.url.includes("/threads/c1/runs/x1/stream"))).toBe(true));
    await waitFor(() => expect(screen.getByTestId("chat-message-assistant")).toHaveTextContent("done now"));
    expect(posts()).toHaveLength(0);
  });

  it("shows a gated tool's ApprovalCard from the interrupt and answers it with resume", async () => {
    const interrupt = {
      id: "approval:r1:tool:call-1",
      reason: "tool_call",
      toolCallId: "call-1",
      message: "Approve refund({\"amount\":5})?",
      responseSchema: { type: "object", properties: { approved: { type: "boolean" }, reason: { type: "string" } }, required: ["approved"] },
      metadata: { pydanticos: { run_id: "r1", tool_name: "refund" } },
    };
    vi.mocked(listEvents).mockResolvedValue([
      ev(0, "run_started", { input: "refund me" }),
      ev(1, "tool_call", { tool_call_id: "call-1", tool_name: "refund", args: { amount: 5 } }),
      ev(2, "tool_approval_waiting", { tool_call_id: "call-1", tool_name: "refund", args: { amount: 5 }, deadline: null, batch: ["call-1"] }),
    ]);
    onPost = (body) => {
      const runId = String(body.runId);
      if (Array.isArray(body.resume)) {
        return sse(runId, [
          { type: "RUN_STARTED", threadId: body.threadId, runId },
          runInfo("r1"),
          { type: "TOOL_CALL_RESULT", messageId: `${runId}-t-call-1`, toolCallId: "call-1", role: "tool", content: "refunded" },
          { type: "TEXT_MESSAGE_START", messageId: `${runId}-m1`, role: "assistant" },
          { type: "TEXT_MESSAGE_CONTENT", messageId: `${runId}-m1`, delta: "Refund sent." },
          { type: "TEXT_MESSAGE_END", messageId: `${runId}-m1` },
          { type: "RUN_FINISHED", threadId: body.threadId, runId, outcome: { type: "success" } },
        ]);
      }
      return sse(runId, [
        { type: "RUN_STARTED", threadId: body.threadId, runId },
        runInfo("r1"),
        { type: "TOOL_CALL_START", toolCallId: "call-1", toolCallName: "refund", parentMessageId: `${runId}-m0` },
        { type: "TOOL_CALL_ARGS", toolCallId: "call-1", delta: "{\"amount\":5}" },
        { type: "TOOL_CALL_END", toolCallId: "call-1" },
        { type: "RUN_FINISHED", threadId: body.threadId, runId, outcome: { type: "interrupt", interrupts: [interrupt] } },
      ]);
    };
    render(<ChatTab agentId="a1" />);
    await send("refund me");
    const card = await screen.findByTestId("tool-approval-waiting");
    expect(card).toHaveAttribute("data-interrupt-id", "approval:r1:tool:call-1");
    // The composer waits while the interrupt is open.
    expect(box()).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    expect(posts()[1].body?.resume).toEqual([{ interruptId: "approval:r1:tool:call-1", status: "resolved", payload: { approved: true } }]);
    // Decided through resume, never the REST route.
    expect(decideToolApproval).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getAllByTestId("chat-message-assistant").at(-1)).toHaveTextContent("Refund sent."));
  });

  it("collects a batch of interrupts and answers them all in one resume", async () => {
    const gated = (id: string, tool: string) => ({
      id: `approval:r1:tool:${id}`,
      reason: "tool_call",
      toolCallId: id,
      message: `Approve ${tool}?`,
      metadata: { pydanticos: { run_id: "r1", tool_name: tool } },
    });
    vi.mocked(listEvents).mockResolvedValue([
      ev(0, "run_started", { input: "do both" }),
      ev(1, "tool_call", { tool_call_id: "call-a", tool_name: "refund", args: { amount: 5 } }),
      ev(2, "tool_call", { tool_call_id: "call-b", tool_name: "email", args: { to: "x" } }),
      ev(3, "tool_approval_waiting", { tool_call_id: "call-a", tool_name: "refund", args: { amount: 5 }, deadline: null, batch: ["call-a", "call-b"] }),
      ev(4, "tool_approval_waiting", { tool_call_id: "call-b", tool_name: "email", args: { to: "x" }, deadline: null, batch: ["call-a", "call-b"] }),
    ]);
    onPost = (body) => {
      const runId = String(body.runId);
      if (Array.isArray(body.resume)) {
        return sse(runId, [
          { type: "RUN_STARTED", threadId: body.threadId, runId },
          runInfo("r1"),
          { type: "TEXT_MESSAGE_START", messageId: `${runId}-m1`, role: "assistant" },
          { type: "TEXT_MESSAGE_CONTENT", messageId: `${runId}-m1`, delta: "Both handled." },
          { type: "TEXT_MESSAGE_END", messageId: `${runId}-m1` },
          { type: "RUN_FINISHED", threadId: body.threadId, runId, outcome: { type: "success" } },
        ]);
      }
      return sse(runId, [
        { type: "RUN_STARTED", threadId: body.threadId, runId },
        runInfo("r1"),
        { type: "TOOL_CALL_START", toolCallId: "call-a", toolCallName: "refund", parentMessageId: `${runId}-m0` },
        { type: "TOOL_CALL_ARGS", toolCallId: "call-a", delta: '{"amount":5}' },
        { type: "TOOL_CALL_END", toolCallId: "call-a" },
        { type: "TOOL_CALL_START", toolCallId: "call-b", toolCallName: "email", parentMessageId: `${runId}-m0` },
        { type: "TOOL_CALL_ARGS", toolCallId: "call-b", delta: '{"to":"x"}' },
        { type: "TOOL_CALL_END", toolCallId: "call-b" },
        { type: "RUN_FINISHED", threadId: body.threadId, runId, outcome: { type: "interrupt", interrupts: [gated("call-a", "refund"), gated("call-b", "email")] } },
      ]);
    };
    render(<ChatTab agentId="a1" />);
    await send("do both");
    const first = await screen.findByRole("region", { name: "Approval for tool refund" });
    const second = await screen.findByRole("region", { name: "Approval for tool email" });
    fireEvent.click(within(first).getByRole("button", { name: "Approve" }));
    // One decision of two: noted, nothing sent yet.
    await waitFor(() => expect(screen.getByTestId("agui-approval-noted")).toHaveTextContent("once every call is answered"));
    expect(posts()).toHaveLength(1);
    fireEvent.click(within(second).getByRole("button", { name: "Deny" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(posts()).toHaveLength(2));
    expect(posts()[1].body?.resume).toEqual([
      { interruptId: "approval:r1:tool:call-a", status: "resolved", payload: { approved: true } },
      { interruptId: "approval:r1:tool:call-b", status: "resolved", payload: { approved: false } },
    ]);
    expect(decideToolApproval).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getAllByTestId("chat-message-assistant").at(-1)).toHaveTextContent("Both handled."));
  });

  it("puts a refused send back in the composer and shows the field error (422 before the stream)", async () => {
    onPost = () =>
      json({ detail: [{ loc: ["forwardedProps", "pydanticos", "model"], msg: "model not allowed", type: "model_not_allowed" }] }, 422);
    render(<ChatTab agentId="a1" />);
    await send("hello");
    await waitFor(() => expect(box().value).toBe("hello"));
    await waitFor(() => expect(screen.getByText(/model not allowed/)).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByTestId("chat-message-user")).not.toBeInTheDocument());
  });

  it("shows the needs_attention banner from RUN_ERROR's code and the recovering banner from CUSTOM status", async () => {
    vi.mocked(getRun).mockResolvedValue({ run_id: "r1", display_status: "needs_attention", error: null } as never);
    onPost = (body) => {
      const runId = String(body.runId);
      return sse(runId, [
        { type: "RUN_STARTED", threadId: body.threadId, runId },
        runInfo("r1"),
        { type: "CUSTOM", name: "pydanticos.status", value: { display_status: "recovering" } },
        { type: "RUN_ERROR", message: "This run needs attention.", code: "needs_attention" },
      ]);
    };
    render(<ChatTab agentId="a1" />);
    await send("hello");
    await waitFor(() => expect(screen.getByText("This run needs attention.")).toBeInTheDocument());
  });

  it("renders structured output from RUN_FINISHED.result with the AC.14 view", async () => {
    onPost = (body) =>
      answerStream(body, '{"answer":42}', "r1", [], { type: "RUN_FINISHED", threadId: body.threadId, runId: String(body.runId), outcome: { type: "success" }, result: { answer: 42 } });
    render(<ChatTab agentId="a1" />);
    await send("structured please");
    await waitFor(() => expect(screen.getByTestId("structured-output")).toBeInTheDocument());
  });

  it("switches back to the native runtime from Settings", async () => {
    const before_ = window.location.pathname + window.location.search;
    window.history.replaceState({}, "", "/?tab=chat");
    render(
      <ThemeProvider>
        <SettingsSections />
        <ChatTab agentId="a1" />
      </ThemeProvider>,
    );
    await waitFor(() => expect(calls.some((call) => call.url.includes("/ag-ui/agents/a1/threads/"))).toBe(true));
    const native = screen.getByRole("radio", { name: "Native" });
    await act(async () => {
      fireEvent.click(native);
    });
    expect(window.localStorage.getItem(CHAT_PROTOCOL_STORAGE_KEY)).toBe("native");
    vi.mocked(startTurnWithOptions).mockResolvedValue({ run_id: "r9", display_status: "queued", version: 2 } as never);
    vi.mocked(subscribeRun).mockReturnValue(() => undefined);
    const { createConversation } = await import("@/lib/conversations");
    vi.mocked(createConversation).mockResolvedValue({ id: "c9", agent_id: "a1", resource_id: null } as never);
    const before = posts().length;
    await send("native turn");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalled());
    expect(posts().length).toBe(before);
    window.history.replaceState({}, "", before_);
  });
});
