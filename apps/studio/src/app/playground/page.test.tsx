import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatTab } from "@/components/agentPage/ChatTab";
import { AgentLayout } from "@/components/agentPage/AgentLayout";
import { api } from "@/lib/api";
import {
  createConversation,
  getConversation,
  listConversations,
  renameConversation,
  type Conversation,
  type ConversationRun,
} from "@/lib/conversations";
import {
  getRun,
  listEvents,
  startRun,
  subscribeRun,
  type Run,
  type RunEvent,
  type RunEventData,
  type SubscribeHandlers,
} from "@/lib/runs";

import {
  startTurnWithOptions as startTurn,
  TurnStartError,
} from "@/components/playground/turnApi";
import { fetchModels, type ModelOption } from "@/lib/metrics";
import { getRequestContext, validateRequestContext } from "@/lib/requestContext";
import type { AgentSpec, AgentVersion } from "@/lib/api";
import { routes } from "@/lib/routes";

// The agent page's switcher navigates with the router. The pathname stays null (as outside the app
// router) unless a test sets it.
const navigation = vi.hoisted(() => ({ pathname: null as string | null, push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useParams: () => ({}),
  useRouter: () => ({ push: navigation.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

vi.mock("@/lib/api", () => ({
  api: { agents: { list: vi.fn(), get: vi.fn() }, versions: { list: vi.fn() } },
}));

vi.mock("@/components/playground/turnApi", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/components/playground/turnApi")>();
  return { ...original, startTurnWithOptions: vi.fn() };
});

vi.mock("@/lib/metrics", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/metrics")>();
  return { ...original, fetchModels: vi.fn() };
});

vi.mock("@/lib/requestContext", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/requestContext")>();
  return { ...original, getRequestContext: vi.fn(), validateRequestContext: vi.fn() };
});

vi.mock("@/lib/conversations", () => ({
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  listConversations: vi.fn(),
  renameConversation: vi.fn(),
}));

vi.mock("@/lib/runs", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/runs")>();
  return {
    ...original,
    startRun: vi.fn(),
    getRun: vi.fn(),
    listEvents: vi.fn(),
    subscribeRun: vi.fn(),
  };
});

const agents = [
  {
    id: "agent-1",
    name: "Support assistant",
    draft_spec: {
      spec_version: 1 as const,
      name: "Support assistant",
      instructions: "Help customers",
      model: { provider: "openai", name: "gpt-4.1-mini" },
      tools: ["create_ticket"],
    },
    latest_version: 1,
  },
  {
    id: "agent-2",
    name: "Draft helper",
    draft_spec: {
      spec_version: 1 as const,
      name: "Draft helper",
      instructions: "Draft only",
      model: { provider: "openai", name: "gpt-4.1-mini" },
      tools: [],
    },
    latest_version: null,
  },
  {
    id: "agent-3",
    name: "Published helper",
    draft_spec: {
      spec_version: 1 as const,
      name: "Published helper",
      instructions: "Also published",
      model: { provider: "openai", name: "gpt-4.1-mini" },
      tools: [],
    },
    latest_version: 2,
  },
];

const specOf = (name: string, model = { provider: "openai", name: "gpt-4.1-mini" }, extra: object = {}): AgentSpec =>
  ({ spec_version: 1, name, instructions: "Help", model, tools: [], ...extra }) as AgentSpec;

const versionOf = (agentId: string, version: number, spec: AgentSpec): AgentVersion => ({
  agent_id: agentId,
  version,
  spec,
  spec_hash: `hash-${version}`,
  created_at: "2026-11-12T00:00:00Z",
  created_by: "dev",
});

const versionsByAgent: Record<string, AgentVersion[]> = {
  "agent-1": [versionOf("agent-1", 1, specOf("Support assistant"))],
  "agent-3": [
    versionOf("agent-3", 1, specOf("Published helper")),
    versionOf("agent-3", 2, specOf("Published helper")),
  ],
};

const modelList: ModelOption[] = [
  { id: "openai:gpt-4.1-mini", provider: "openai", name: "gpt-4.1-mini", wildcard: false, source: "allowlist", description: null },
  { id: "openai:gpt-4.1", provider: "openai", name: "gpt-4.1", wildcard: false, source: "allowlist", description: null },
  { id: "anthropic:*", provider: "anthropic", name: "*", wildcard: true, source: "allowlist", description: null },
  { id: "test:echo", provider: "test", name: "echo", wildcard: false, source: "scripted", description: "Echoes" },
];

const run = (id: string, display_status: Run["display_status"] = "running"): Run => ({
  run_id: id,
  status: display_status,
  display_status,
  agent_id: "agent-1",
  version: 1,
  input: "",
  output: null,
  error: null,
  heartbeat_at: null,
  created_at: "2026-11-12T00:00:00Z",
  updated_at: "2026-11-12T00:00:00Z",
  trace_id: null,
  conversation_id: null,
  turn: null,
});

const event = (
  seq: number,
  type: RunEvent["type"],
  data: unknown,
): RunEvent => ({ seq, type, data: data as RunEventData, ts: "2026-11-12T00:00:00Z" });

let handlers: SubscribeHandlers;
const unsubscribe = vi.fn();

function composer() {
  return screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
}

/** The agent and conversation the route names (`/agents/:id/chat[/:conversationId]`). */
function routeChat() {
  const match = /^\/agents\/([^/]+)\/chat(?:\/([^/]+))?/.exec(window.location.pathname);
  return { agentId: match ? decodeURIComponent(match[1]) : "agent-1", conversationId: match?.[2] ? decodeURIComponent(match[2]) : null };
}

/** The conversation id in the address bar (it is in the path since SL.6, no longer `?conversation=`). */
function urlConversation() {
  return routeChat().conversationId;
}

async function renderReady() {
  const { agentId, conversationId } = routeChat();
  const view = render(<ChatTab key={`${agentId}/${conversationId}`} agentId={agentId} conversationId={conversationId} />);
  await screen.findByRole("combobox", { name: "Version" });
  await waitFor(() => expect(api.agents.get).toHaveBeenCalledWith(agentId));
  return view;
}

async function sendMessage(text = "Please create a ticket") {
  const callsBeforeSend = vi.mocked(startTurn).mock.calls.length;
  fireEvent.change(composer(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() =>
    expect(startTurn).toHaveBeenCalledTimes(callsBeforeSend + 1),
  );
}

const turnRun = (
  run_id: string,
  turn: number,
  input: string,
  display_status: ConversationRun["display_status"],
): ConversationRun => ({
  run_id,
  agent_id: "agent-1",
  version: 1,
  status: display_status,
  display_status,
  input,
  output: null,
  error: null,
  heartbeat_at: null,
  created_at: "2026-11-12T00:00:00Z",
  updated_at: "2026-11-12T00:00:00Z",
  trace_id: null,
  conversation_id: "conv-1",
  turn,
});

function mockConversation(runs: ConversationRun[], events: Record<string, RunEvent[]>, agentId = "agent-3") {
  vi.mocked(getConversation).mockResolvedValue({
    id: "conv-1",
    agent_id: agentId,
    title: "Earlier chat",
    turn_count: runs.length,
    created_at: "2026-11-12T00:00:00Z",
    updated_at: "2026-11-12T00:00:00Z",
    runs,
  });
  vi.mocked(listEvents).mockImplementation(async (runId) => events[runId] ?? []);
}

const answered = (input: string, answer: string): RunEvent[] => [
  event(0, "run_started", { input }),
  event(1, "model_message", { model_request: 0, text: answer }),
  event(2, "run_completed", { output: answer }),
];

beforeEach(() => {
  window.history.replaceState({}, "", routes.agentChat("agent-1"));
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.mocked(api.agents.list).mockResolvedValue(agents);
  vi.mocked(api.agents.get).mockImplementation(async (id) => agents.find((a) => a.id === id)!);
  vi.mocked(api.versions.list).mockImplementation(async (agentId) => versionsByAgent[agentId] ?? []);
  vi.mocked(fetchModels).mockResolvedValue(modelList);
  vi.mocked(getRequestContext).mockImplementation(async (agentId, version) => ({
    agent_id: agentId,
    version: typeof version === "number" ? version : null,
    draft: version === "draft",
    fields: [],
    schema: { type: "object" as const, properties: {} },
  }));
  vi.mocked(validateRequestContext).mockImplementation(async (_agentId, value) => value);
  vi.mocked(startRun).mockImplementation(async (_agentId, runId) => run(runId));
  vi.mocked(createConversation).mockImplementation(async (agentId, title, id) => ({
    id: id ?? "conv-new",
    agent_id: agentId,
    title,
    turn_count: 0,
    created_at: "2026-11-12T00:00:00Z",
    updated_at: "2026-11-12T00:00:00Z",
  }));
  let turnNumber = 0;
  vi.mocked(startTurn).mockImplementation(async (conversationId, runId) => ({
    run_id: runId,
    status: "queued",
    display_status: "queued",
    agent_id: "agent-1",
    version: 1,
    conversation_id: conversationId,
    turn: ++turnNumber,
  }));
  vi.mocked(getRun).mockImplementation(async (runId) => run(runId));
  vi.mocked(listEvents).mockResolvedValue([]);
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null });
  vi.mocked(subscribeRun).mockImplementation((_runId, nextHandlers) => {
    handlers = nextHandlers;
    return unsubscribe;
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PlaygroundPage", () => {
  it("creates a conversation, then starts the turn with a UUID before subscribing to it", async () => {
    await renderReady();
    await sendMessage("Hello");

    expect(createConversation).toHaveBeenCalledTimes(1);
    const [agentId, title, conversationId] = vi.mocked(createConversation).mock.calls[0];
    // The API titles the conversation from its first turn.
    expect([agentId, title]).toEqual(["agent-1", null]);
    const [turnConversation, runId, input] = vi.mocked(startTurn).mock.calls[0];
    expect(turnConversation).toBe(conversationId);
    expect(runId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(input).toBe("Hello");
    expect(startRun).not.toHaveBeenCalled();
    expect(subscribeRun).toHaveBeenCalledWith(runId, expect.any(Object));
    expect(vi.mocked(startTurn).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(subscribeRun).mock.invocationCallOrder[0],
    );
    expect(urlConversation()).toBe(conversationId);
  });

  it("sends the second message to the same conversation and keeps both turns in the thread", async () => {
    await renderReady();
    await sendMessage("My name is Ada");
    const firstRun = vi.mocked(startTurn).mock.calls[0][1];
    act(() => {
      handlers.onEvent?.(event(0, "run_started", { input: "My name is Ada" }));
      handlers.onEvent?.(event(1, "model_message", { model_request: 0, text: "Hello Ada" }));
      handlers.onEvent?.(event(2, "run_completed", { output: "Hello Ada" }));
    });
    await waitFor(() => expect(composer()).toBeEnabled());

    await sendMessage("What is my name?");
    expect(createConversation).toHaveBeenCalledTimes(1);
    const [[conv1], [conv2, secondRun]] = vi.mocked(startTurn).mock.calls;
    expect(conv2).toBe(conv1);
    expect(secondRun).not.toBe(firstRun);
    expect(subscribeRun).toHaveBeenLastCalledWith(secondRun, expect.any(Object));

    expect(screen.getByText("My name is Ada")).toBeInTheDocument();
    expect(screen.getByText("Hello Ada")).toBeInTheDocument();
    expect(screen.getByText("What is my name?")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "View run" })).toHaveAttribute(
      "href",
      routes.run(secondRun),
    );
  });

  it("keeps the composer disabled while a turn runs", async () => {
    await renderReady();
    await sendMessage();

    await waitFor(() => expect(composer()).toBeDisabled());
    act(() => {
      handlers.onStatus?.({ display_status: "running" });
    });
    expect(composer()).toBeDisabled();
    act(() => {
      handlers.onEvent?.(event(2, "run_completed", {}));
    });
    await waitFor(() => expect(composer()).toBeEnabled());
  });

  it.each([
    ["completed", "run_completed", { output: "Done" }, "Completed", null],
    ["failed", "run_failed", { status: "failed", error: "Provider unavailable" }, "Failed", "Provider unavailable"],
    [
      "needs attention",
      "run_failed",
      { status: "needs_attention", error: "Side effect outcome is unknown" },
      "Needs Attention",
      "Side effect outcome is unknown",
    ],
  ] as const)(
    "shows %s from a live terminal event without a status frame",
    async (_caseName, type, data, expectedStatus, expectedError) => {
      await renderReady();
      await sendMessage();

      act(() => {
        handlers.onEvent?.(event(2, type, data));
      });

      expect(await screen.findByText(expectedStatus)).toBeInTheDocument();
      await waitFor(() => expect(composer()).toBeEnabled());
      if (expectedError) {
        expect(screen.getByRole("alert")).toHaveTextContent(expectedError);
      }
    },
  );

  it("falls back to failed when a run_failed event has an invalid status", async () => {
    await renderReady();
    await sendMessage();

    act(() => {
      handlers.onEvent?.(event(2, "run_failed", { status: "completed", error: "Invalid status" }));
    });

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Invalid status");
  });

  it("shows recovery only while the server display status is recovering", async () => {
    await renderReady();
    await sendMessage();

    act(() => {
      handlers.onStatus?.({ display_status: "recovering" });
    });
    expect(screen.getAllByText(/worker is restarting/i)).not.toHaveLength(0);

    act(() => {
      handlers.onStatus?.({ display_status: "running" });
    });
    expect(screen.queryAllByText(/worker is restarting/i)).toHaveLength(0);
  });

  it("rebuilds the whole thread from ?conversation= and re-subscribes to the in-flight turn", async () => {
    mockConversation(
      [
        { ...turnRun("run-1", 1, "My name is Ada", "completed"), output: "Hello Ada" },
        turnRun("run-2", 2, "What is my name?", "running"),
      ],
      {
        "run-1": answered("My name is Ada", "Hello Ada"),
        "run-2": [event(0, "run_started", { input: "What is my name?" })],
      },
    );
    window.history.replaceState({}, "", routes.agentChat("agent-3", { conversation: "conv-1" }));
    await renderReady();

    expect(await screen.findByText("Hello Ada")).toBeInTheDocument();
    expect(screen.getByText("My name is Ada")).toBeInTheDocument();
    expect(screen.getByText("What is my name?")).toBeInTheDocument();
    expect(getConversation).toHaveBeenCalledWith("conv-1");
    expect(listEvents).toHaveBeenCalledWith("run-1");
    expect(listEvents).toHaveBeenCalledWith("run-2");
    await waitFor(() => expect(subscribeRun).toHaveBeenCalledTimes(1));
    expect(vi.mocked(subscribeRun).mock.calls[0][0]).toBe("run-2");
    expect(vi.mocked(subscribeRun).mock.calls[0][2]).toEqual({ initialLastSeq: 0 });
    expect(startTurn).not.toHaveBeenCalled();
    expect(createConversation).not.toHaveBeenCalled();
    expect(composer()).toBeDisabled();
    // Was the Agent picker showing agent-3; the agent is the route's now, loaded for the chat.
    expect(api.agents.get).toHaveBeenCalledWith("agent-3");
    expect(screen.queryByRole("combobox", { name: "Agent" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View run" })).toHaveAttribute("href", routes.run("run-2"));

    act(() => {
      handlers.onEvent?.(event(1, "model_message", { model_request: 0, text: "You are Ada" }));
      handlers.onEvent?.(event(2, "run_completed", { output: "You are Ada" }));
    });
    expect(await screen.findByText("You are Ada")).toBeInTheDocument();
    await waitFor(() => expect(composer()).toBeEnabled());
    await sendMessage("Thanks");
    expect(vi.mocked(startTurn).mock.calls[0][0]).toBe("conv-1");
  });

  it("re-posts a turn that was recorded but never started, with the same run_id", async () => {
    mockConversation([turnRun("run-1", 1, "Hello", "queued")], {});
    window.history.replaceState({}, "", routes.agentChat("agent-3", { conversation: "conv-1" }));
    await renderReady();

    await waitFor(() =>
      expect(startTurn).toHaveBeenCalledWith("conv-1", "run-1", "Hello", {
        version: 1,
        model: null,
        request_context: null,
      }),
    );
    await waitFor(() =>
      expect(subscribeRun).toHaveBeenCalledWith("run-1", expect.any(Object), { initialLastSeq: -1 }),
    );
    expect(vi.mocked(startTurn).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(subscribeRun).mock.invocationCallOrder[0],
    );
    expect(await screen.findByText("Hello")).toBeInTheDocument();
  });

  it("does not subscribe when every turn of the loaded conversation is finished", async () => {
    mockConversation(
      [{ ...turnRun("run-1", 1, "Hi", "completed"), output: "Hey" }],
      { "run-1": answered("Hi", "Hey") },
    );
    window.history.replaceState({}, "", routes.agentChat("agent-3", { conversation: "conv-1" }));
    await renderReady();

    expect(await screen.findByText("Hey")).toBeInTheDocument();
    expect(subscribeRun).not.toHaveBeenCalled();
    await waitFor(() => expect(composer()).toBeEnabled());
  });

  it("starts a new conversation and clears the URL when switching agents or pressing New conversation", async () => {
    // The agent page: header switcher plus the Chat tab, as `app/agents/[id]/layout.tsx` renders them.
    navigation.pathname = routes.agentChat("agent-1");
    navigation.push.mockReset();
    const view = render(
      <AgentLayout agentId="agent-1">
        <ChatTab agentId="agent-1" />
      </AgentLayout>,
    );
    await waitFor(() => expect(composer()).toBeEnabled());
    await sendMessage();
    expect(urlConversation()).not.toBeNull();

    // Was the Agent picker: the breadcrumb switcher goes to the other agent's Chat tab, with no
    // conversation and none of this agent's options.
    fireEvent.click(await screen.findByRole("combobox", { name: "Switch agent" }));
    fireEvent.click(await screen.findByRole("option", { name: "Published helper" }));
    expect(navigation.push).toHaveBeenCalledWith(routes.agentChat("agent-3"));
    const target = navigation.push.mock.calls[0][0] as string;

    // Next renders that route: a new agent segment, so the first tab unmounts and agent-3's opens.
    view.unmount();
    navigation.pathname = null;
    window.history.replaceState({}, "", target);
    const second = await renderReady();
    expect(unsubscribe).toHaveBeenCalledTimes(1);
    expect(api.agents.get).toHaveBeenLastCalledWith("agent-3");
    await waitFor(() => expect(listConversations).toHaveBeenLastCalledWith(expect.objectContaining({ agentId: "agent-3" })));
    expect(urlConversation()).toBeNull();
    expect(screen.getByRole("option", { name: "Latest (v2)" })).toBeInTheDocument();
    expect(screen.queryByText("Please create a ticket")).not.toBeInTheDocument();

    await sendMessage("Fresh start");
    expect(createConversation).toHaveBeenCalledTimes(2);
    expect(vi.mocked(createConversation).mock.calls[1][0]).toBe("agent-3");
    act(() => {
      handlers.onEvent?.(event(2, "run_completed", {}));
    });
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    expect(screen.queryByText("Fresh start")).not.toBeInTheDocument();
    expect(urlConversation()).toBeNull();

    second.unmount();
    expect(unsubscribe).toHaveBeenCalledTimes(2);
  });

  it("no longer says the playground has no conversation memory", async () => {
    await renderReady();
    expect(screen.queryByText(/conversation memory/i)).not.toBeInTheDocument();
  });

  it("renders a tool call name and result through assistant-ui message parts", async () => {
    mockConversation([{ ...turnRun("tool-run", 1, "Open a ticket", "completed") }], {
      "tool-run": [
        event(0, "run_started", { input: "Open a ticket" }),
        event(1, "model_message", {
          model_request: 0,
          text: "",
          message: { kind: "response" },
        }),
        event(2, "tool_call", {
          tool_call_id: "call-1",
          tool_name: "tickets__create",
          args: { title: "Printer broken" },
        }),
        event(3, "tool_result", {
          tool_call_id: "call-1",
          tool_name: "tickets__create",
          status: "ok",
          content: { ticket_id: "T-42" },
        }),
        event(4, "model_message", {
          model_request: 1,
          text: "Ticket opened",
        }),
      ],
    });
    window.history.replaceState({}, "", routes.agentChat("agent-3", { conversation: "conv-1" }));

    await renderReady();

    expect(await screen.findByText("tickets__create")).toBeInTheDocument();
    expect(screen.getByText(/"ticket_id": "T-42"/)).toBeInTheDocument();
  });

  it("renders assistant markdown without exposing raw markup", async () => {
    mockConversation([turnRun("markdown-run", 1, "What is my ticket?", "completed")], {
      "markdown-run": [
        event(0, "run_started", { input: "What is my ticket?" }),
        event(1, "model_message", {
          model_request: 0,
          text: "Created **TCK-42**. See [details](https://example.com/tickets/42).",
        }),
      ],
    });
    window.history.replaceState({}, "", routes.agentChat("agent-3", { conversation: "conv-1" }));

    await renderReady();

    const ticket = await screen.findByText("TCK-42");
    expect(ticket.tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*TCK-42\*\*/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "details" })).toHaveAttribute(
      "href",
      "https://example.com/tickets/42",
    );
  });

  describe("conversation rail", () => {
    const listed = (id: string, title: string | null, agent_id = "agent-1"): Conversation => ({
      id,
      agent_id,
      title,
      turn_count: 1,
      last_turn_run_id: `run-of-${id}`,
      last_turn_status: "completed",
      last_turn_display_status: "completed",
      created_at: "2026-11-12T00:00:00Z",
      updated_at: "2026-11-12T00:00:00Z",
    });

    it("lists the selected agent's conversations and reloads them after a turn starts", async () => {
      vi.mocked(listConversations).mockResolvedValue({
        items: [listed("conv-1", "Earlier chat")],
        next_before: null,
      });
      await renderReady();
      expect(await screen.findByRole("button", { name: /^Earlier chat/ })).toBeInTheDocument();
      expect(listConversations).toHaveBeenCalledWith({ agentId: "agent-1", limit: 30 });
      const calls = vi.mocked(listConversations).mock.calls.length;
      await sendMessage("Hello");
      await waitFor(() => expect(vi.mocked(listConversations).mock.calls.length).toBeGreaterThan(calls));
    });

    it("loads a picked conversation into the thread and the URL", async () => {
      vi.mocked(listConversations).mockResolvedValue({
        items: [listed("conv-1", "Earlier chat", "agent-1")],
        next_before: null,
      });
      mockConversation(
        [{ ...turnRun("run-1", 1, "My name is Ada", "completed"), output: "Hello Ada" }],
        { "run-1": answered("My name is Ada", "Hello Ada") },
      );
      vi.mocked(getConversation).mockResolvedValueOnce({
        ...(await getConversation("conv-1")),
        agent_id: "agent-1",
      });
      await renderReady();
      fireEvent.click(await screen.findByRole("button", { name: /^Earlier chat/ }));

      expect(await screen.findByText("Hello Ada")).toBeInTheDocument();
      expect(urlConversation()).toBe("conv-1");
      await waitFor(() =>
        expect(screen.getByRole("button", { name: /^Earlier chat/ })).toHaveAttribute("aria-current", "true"),
      );
      expect(subscribeRun).not.toHaveBeenCalled();
    });

    it("switching away from a streaming turn stops its subscription without cancelling the turn", async () => {
      vi.mocked(listConversations).mockResolvedValue({
        items: [listed("conv-1", "Earlier chat")],
        next_before: null,
      });
      await renderReady();
      await sendMessage("Long question");
      const [streamingConversation, streamingRun] = vi.mocked(startTurn).mock.calls[0];
      act(() => {
        handlers.onEvent?.(event(0, "run_started", { input: "Long question" }));
      });

      mockConversation(
        [{ ...turnRun("run-1", 1, "My name is Ada", "completed"), output: "Hello Ada" }],
        { "run-1": answered("My name is Ada", "Hello Ada") },
        "agent-1",
      );
      fireEvent.click(await screen.findByRole("button", { name: /^Earlier chat/ }));
      expect(unsubscribe).toHaveBeenCalledTimes(1);
      expect(await screen.findByText("Hello Ada")).toBeInTheDocument();
      expect(screen.queryByText("Long question")).not.toBeInTheDocument();
      await waitFor(() => expect(composer()).toBeEnabled());

      // Going back rebuilds the streaming thread from the API and follows the same run again.
      vi.mocked(getConversation).mockResolvedValue({
        id: streamingConversation,
        agent_id: "agent-1",
        title: "Long question",
        turn_count: 1,
        created_at: "2026-11-12T00:00:00Z",
        updated_at: "2026-11-12T00:00:00Z",
        runs: [{ ...turnRun(streamingRun, 1, "Long question", "running"), conversation_id: streamingConversation }],
      });
      vi.mocked(listEvents).mockResolvedValue([event(0, "run_started", { input: "Long question" })]);
      vi.mocked(listConversations).mockResolvedValue({
        items: [listed(streamingConversation, "Long question"), listed("conv-1", "Earlier chat")],
        next_before: null,
      });
      act(() => {
        handlers.onEvent?.(event(5, "run_completed", {})); // a late event of the stopped subscription
      });
      fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
      fireEvent.click(await screen.findByRole("button", { name: /^Long question/ }));
      expect(await screen.findByText("Long question")).toBeInTheDocument();
      await waitFor(() =>
        expect(subscribeRun).toHaveBeenLastCalledWith(streamingRun, expect.any(Object), { initialLastSeq: 0 }),
      );
      expect(startTurn).toHaveBeenCalledTimes(1);
    });

    it("ignores a slow load once another conversation was picked", async () => {
      vi.mocked(listConversations).mockResolvedValue({
        items: [listed("conv-a", "First"), listed("conv-b", "Second")],
        next_before: null,
      });
      let releaseFirst: (value: Awaited<ReturnType<typeof getConversation>>) => void = () => {};
      vi.mocked(getConversation).mockImplementation((id) =>
        id === "conv-a"
          ? new Promise((resolve) => {
              releaseFirst = resolve;
            })
          : Promise.resolve({
              id: "conv-b",
              agent_id: "agent-1",
              title: "Second",
              turn_count: 1,
              created_at: "2026-11-12T00:00:00Z",
              updated_at: "2026-11-12T00:00:00Z",
              runs: [{ ...turnRun("run-b", 1, "Second question", "completed"), output: "B" }],
            }),
      );
      await renderReady();
      fireEvent.click(await screen.findByRole("button", { name: /^First/ }));
      fireEvent.click(screen.getByRole("button", { name: /^Second/ }));
      expect(await screen.findByText("Second question")).toBeInTheDocument();
      await act(async () => {
        releaseFirst({
          id: "conv-a",
          agent_id: "agent-1",
          title: "First",
          turn_count: 1,
          created_at: "2026-11-12T00:00:00Z",
          updated_at: "2026-11-12T00:00:00Z",
          runs: [turnRun("run-a", 1, "First question", "running")],
        });
      });
      expect(screen.queryByText("First question")).not.toBeInTheDocument();
      expect(subscribeRun).not.toHaveBeenCalled();
      expect(urlConversation()).toBe("conv-b");
      expect(renameConversation).not.toHaveBeenCalled();
    });

    it("finishes a send whose conversation is still being created when the user switches threads", async () => {
      vi.mocked(listConversations).mockResolvedValue({
        items: [listed("conv-1", "Earlier chat")],
        next_before: null,
      });
      mockConversation(
        [{ ...turnRun("run-1", 1, "My name is Ada", "completed"), output: "Hello Ada" }],
        { "run-1": answered("My name is Ada", "Hello Ada") },
        "agent-1",
      );
      let finishCreate: () => void = () => {};
      vi.mocked(createConversation).mockImplementationOnce(
        (agentId, title, id) =>
          new Promise((resolve) => {
            finishCreate = () =>
              resolve({
                id: id ?? "conv-new",
                agent_id: agentId,
                title,
                turn_count: 0,
                created_at: "2026-11-12T00:00:00Z",
                updated_at: "2026-11-12T00:00:00Z",
              });
          }),
      );
      await renderReady();
      fireEvent.change(composer(), { target: { value: "Sent before switching" } });
      fireEvent.click(screen.getByRole("button", { name: "Send message" }));
      await waitFor(() => expect(createConversation).toHaveBeenCalledTimes(1));
      const createdId = vi.mocked(createConversation).mock.calls[0][2];

      fireEvent.click(await screen.findByRole("button", { name: /^Earlier chat/ }));
      expect(await screen.findByText("Hello Ada")).toBeInTheDocument();
      await act(async () => finishCreate());

      // The turn is started on the new conversation (no empty conversation is left behind) ...
      await waitFor(() => expect(startTurn).toHaveBeenCalledTimes(1));
      const [turnConversation, , input] = vi.mocked(startTurn).mock.calls[0];
      expect([turnConversation, input]).toEqual([createdId, "Sent before switching"]);
      // ... but the thread the user switched to stays on screen and is not taken over.
      expect(screen.queryByText("Sent before switching")).not.toBeInTheDocument();
      expect(screen.getByText("Hello Ada")).toBeInTheDocument();
      expect(urlConversation()).toBe("conv-1");
      expect(subscribeRun).not.toHaveBeenCalled();
      await waitFor(() => expect(composer()).toBeEnabled());
    });
  });
});

describe("PlaygroundPage run options (P0.19)", () => {
  const tierInfo = (agentId: string, version: unknown) => ({
    agent_id: agentId,
    version: typeof version === "number" ? version : null,
    draft: version === "draft",
    fields: [
      { name: "tier", type: "string" as const, required: false, description: "Customer tier", max_length: 20, values: [] },
    ],
    schema: { type: "object" as const, properties: { tier: { type: "string" as const, title: "tier" } } },
  });

  const lastOptions = () => vi.mocked(startTurn).mock.calls.at(-1)?.[3];

  async function modelsReady() {
    await screen.findByRole("option", { name: "openai:gpt-4.1" });
  }

  function choose(name: string, value: string) {
    fireEvent.change(screen.getByRole("combobox", { name }), { target: { value } });
  }

  function answer(runId: string, modelId: string, cost: string | null, tokens: [number, number]) {
    act(() => {
      handlers.onEvent?.(event(0, "run_started", { input: "x" }));
      handlers.onEvent?.(
        event(1, "model_message", {
          model_request: 0,
          is_final: true,
          text: `answer from ${modelId}`,
          model_id: modelId,
          usage: { input_tokens: tokens[0], output_tokens: tokens[1] },
          cost_usd: cost,
        }),
      );
      handlers.onEvent?.(event(2, "run_completed", { output: `answer from ${modelId}` }));
    });
    return runId;
  }

  beforeEach(() => {
    vi.mocked(startTurn).mockImplementation(async (conversationId, runId, _input, options) => ({
      run_id: runId,
      status: "queued",
      display_status: "queued",
      agent_id: "agent-1",
      version: options.version === "draft" ? null : (options.version ?? 1),
      draft: options.version === "draft",
      model_id: options.model ? `${options.model.provider}:${options.model.name}` : "openai:gpt-4.1-mini",
      model_overridden: options.model !== null,
      conversation_id: conversationId,
      turn: 1,
    }));
  });

  it("sends the version, the model override and the request context with the turn, and keeps them in the URL", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (agentId, version) => tierInfo(agentId, version));
    window.sessionStorage.setItem(
      `eap.playground.context.v1:anon:agent-1`,
      JSON.stringify({ value: { tier: "gold" } }),
    );
    await renderReady();
    await modelsReady();
    choose("Version", "v1");
    choose("Model", "openai:gpt-4.1");
    await sendMessage("Hi");

    expect(lastOptions()).toEqual({
      version: 1,
      model: { provider: "openai", name: "gpt-4.1" },
      request_context: { tier: "gold" },
    });
    const params = new URLSearchParams(window.location.search);
    // Was ?agent=agent-1; the agent is in the path now.
    expect(window.location.pathname).toMatch(/^\/agents\/agent-1\/chat(\/[^/]+)?$/);
    expect(params.get("agent")).toBeNull();
    expect(params.get("version")).toBe("1");
    expect(params.get("model")).toBe("openai:gpt-4.1");
  });

  it("does not send the version's own model as an override and offers no wildcard as a plain model", async () => {
    await renderReady();
    await modelsReady();
    const model = screen.getByRole("combobox", { name: "Model" });
    expect(model).toHaveValue("");
    expect(screen.getByRole("option", { name: "Version default (openai:gpt-4.1-mini)" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "openai:gpt-4.1-mini" })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "anthropic: other model…" })).toBeInTheDocument();
    await sendMessage("Hi");
    expect(lastOptions()).toEqual({ version: null, model: null, request_context: null });
  });

  it("switches model mid-conversation; each turn's badge shows its own model, tokens and cost", async () => {
    await renderReady();
    await modelsReady();
    await sendMessage("First");
    const firstRun = vi.mocked(startTurn).mock.calls[0][1];
    answer(firstRun, "openai:gpt-4.1-mini", "0.0012", [1200, 300]);
    await waitFor(() => expect(composer()).toBeEnabled());

    choose("Model", "test:echo");
    await sendMessage("Second");
    const [, secondRun, , secondOptions] = vi.mocked(startTurn).mock.calls[1];
    expect(vi.mocked(startTurn).mock.calls[0][3].model).toBeNull();
    expect(secondOptions.model).toEqual({ provider: "test", name: "echo" });
    expect(vi.mocked(startTurn).mock.calls[1][0]).toBe(vi.mocked(startTurn).mock.calls[0][0]);
    answer(secondRun, "test:echo", null, [10, 5]);

    await waitFor(() => expect(screen.getAllByTestId("turn-badge")).toHaveLength(2));
    expect(screen.getAllByTestId("turn-badge-model").map((el) => el.textContent)).toEqual([
      "openai:gpt-4.1-mini",
      "test:echo",
    ]);
    expect(screen.getAllByTestId("turn-badge-tokens").map((el) => el.textContent)).toEqual([
      "1,200 in · 300 out tokens",
      "10 in · 5 out tokens",
    ]);
    expect(screen.getAllByTestId("turn-badge-cost").map((el) => el.textContent)).toEqual(["$0.0012", "no price"]);
    expect(screen.getAllByText("override")).toHaveLength(1);
    expect(screen.getAllByRole("link", { name: "Open trace" }).map((el) => el.getAttribute("href"))).toEqual([
      routes.trace(firstRun),
      routes.trace(secondRun),
    ]);
  });

  it("shows a refused model inline under the picker, not as a generic failure", async () => {
    vi.mocked(startTurn).mockRejectedValueOnce(
      new TurnStartError(422, "model 'openai:gpt-4.1' is not allowed on this platform (not in the EAP_ALLOWED_MODELS allowlist)"),
    );
    await renderReady();
    await modelsReady();
    choose("Model", "openai:gpt-4.1");
    await sendMessage("Hi");

    const alert = await screen.findByText(/is not allowed on this platform/);
    expect(alert).toHaveAttribute("id", "playground-model-error");
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByText("Failed to send the message.")).not.toBeInTheDocument();
    expect(screen.queryAllByTestId("turn-footer")).toHaveLength(0);
    expect(screen.getByTestId("unsent-message")).toHaveTextContent("Hi");
    await waitFor(() => expect(composer()).toBeEnabled());

    choose("Model", "test:echo");
    expect(screen.queryByText(/is not allowed on this platform/)).not.toBeInTheDocument();
  });

  it("shows refused context fields inline in the context panel", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (agentId, version) => tierInfo(agentId, version));
    window.sessionStorage.setItem(
      `eap.playground.context.v1:anon:agent-1`,
      JSON.stringify({ value: { tier: "platinum-plus-extra-long" } }),
    );
    vi.mocked(startTurn).mockRejectedValueOnce(
      new TurnStartError(422, [
        { loc: ["body", "request_context", "tier"], msg: "String should have at most 20 characters", type: "string_too_long" },
      ]),
    );
    await renderReady();
    await waitFor(() => expect(getRequestContext).toHaveBeenCalled());
    await sendMessage("Hi");

    const errors = await screen.findByTestId("context-turn-errors");
    expect(errors).toHaveTextContent("tier");
    expect(errors).toHaveTextContent("String should have at most 20 characters");
    expect(screen.getByRole("region", { name: "Request context" })).toBeInTheDocument();
    expect(lastOptions()?.request_context).toEqual({ tier: "platinum-plus-extra-long" });
  });

  it("shows a refused draft inline under the version selector", async () => {
    vi.mocked(startTurn).mockRejectedValueOnce(new TurnStartError(422, "instructions: unknown placeholder {{context.plan}}"));
    await renderReady();
    choose("Version", "draft");
    await sendMessage("Hi");
    expect(lastOptions()?.version).toBe("draft");
    expect(await screen.findByText("The draft cannot run: instructions: unknown placeholder {{context.plan}}")).toHaveAttribute(
      "id",
      "playground-version-error",
    );
  });

  it("shows the draft's own model refusal as a draft problem, not under the picker", async () => {
    vi.mocked(startTurn).mockRejectedValueOnce(
      new TurnStartError(422, "model 'openai:gpt-9' is not allowed on this platform (not in the EAP_ALLOWED_MODELS allowlist)"),
    );
    await renderReady();
    choose("Version", "draft");
    await sendMessage("Hi");
    expect(lastOptions()?.model).toBeNull();
    expect(await screen.findByText(/^The draft cannot run: model 'openai:gpt-9'/)).toHaveAttribute("id", "playground-version-error");
    expect(document.getElementById("playground-model-error")).toBeNull();
  });

  it("shows other refusals inline above the thread", async () => {
    vi.mocked(startTurn).mockRejectedValueOnce(
      new TurnStartError(422, [{ loc: ["body", "input"], msg: "input must be at most 32 KiB in UTF-8", type: "value_error" }]),
    );
    await renderReady();
    await sendMessage("Hi");
    expect(await screen.findByTestId("turn-options-error")).toHaveTextContent("input: input must be at most 32 KiB in UTF-8");
  });

  it("restores agent, version and model from the URL on reload", async () => {
    window.history.replaceState({}, "", routes.agentChat("agent-3", { version: 1, model: "test:echo" }));
    await renderReady();
    expect(api.agents.get).toHaveBeenCalledWith("agent-3");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Version" })).toHaveValue("v1"));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("test:echo"));
    expect(new URLSearchParams(window.location.search).get("model")).toBe("test:echo");
    await sendMessage("Hi");
    expect(vi.mocked(createConversation).mock.calls[0][0]).toBe("agent-3");
    expect(lastOptions()).toEqual({ version: 1, model: { provider: "test", name: "echo" }, request_context: null });
  });

  it("restores a wildcard model's typed name from the URL", async () => {
    window.history.replaceState({}, "", routes.agentChat("agent-1", { model: "anthropic:claude-x" }));
    await renderReady();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("anthropic:*"));
    expect(screen.getByRole("textbox", { name: "Model name" })).toHaveValue("claude-x");
  });

  it("drops a URL model the platform does not offer", async () => {
    window.history.replaceState({}, "", routes.agentChat("agent-1", { model: "openai:gpt-5" }));
    await renderReady();
    expect(await screen.findByText(/openai:gpt-5 is not offered here/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("");
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("model")).toBeNull());
  });

  it("takes a free-text model name for a wildcard provider", async () => {
    await renderReady();
    await modelsReady();
    choose("Model", "anthropic:*");
    expect(screen.getByText("Enter a model name for anthropic")).toBeInTheDocument();
    expect(composer()).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Model name" }), { target: { value: "claude-sonnet-x" } });
    await waitFor(() => expect(composer()).toBeEnabled());
    await sendMessage("Hi");
    expect(lastOptions()?.model).toEqual({ provider: "anthropic", name: "claude-sonnet-x" });
    expect(new URLSearchParams(window.location.search).get("model")).toBe("anthropic:claude-sonnet-x");
  });

  it("runs the draft of an agent that has no published version", async () => {
    // Was choosing agent-2 in the picker; now agent-2's own ChatTab.
    window.history.replaceState({}, "", routes.agentChat("agent-2"));
    await renderReady();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Version" })).toHaveValue("draft"));
    expect(screen.queryByRole("option", { name: /Latest/ })).not.toBeInTheDocument();
    await sendMessage("Hi");
    expect(vi.mocked(createConversation).mock.calls[0][0]).toBe("agent-2");
    expect(lastOptions()?.version).toBe("draft");
    expect(await screen.findByTestId("turn-badge-version")).toHaveTextContent("Draft");
    expect(new URLSearchParams(window.location.search).get("version")).toBe("draft");
  });

  it("shows the output-check state while an agent with output processors runs, then the checked answer", async () => {
    vi.mocked(api.versions.list).mockResolvedValue([
      versionOf("agent-1", 1, specOf("Support assistant", undefined, { output_processors: [{ type: "pii_redact" }] })),
    ]);
    await renderReady();
    await waitFor(() => expect(api.versions.list).toHaveBeenCalled());
    await sendMessage("Hi");
    expect(await screen.findByTestId("turn-output-checking")).toHaveTextContent(
      "The output is being checked before display.",
    );
    const runId = vi.mocked(startTurn).mock.calls[0][1];
    // A delta that arrives anyway (defence in depth) is never shown.
    act(() => {
      handlers.onEvent?.(event(0, "run_started", { input: "Hi" }));
      handlers.onDelta?.({ run_id: runId, model_request: 0, attempt: "a", delta: "UNCHECKED secret@example.com" });
    });
    expect(screen.queryByText(/UNCHECKED/)).not.toBeInTheDocument();
    expect(screen.getByTestId("turn-output-checking")).toBeInTheDocument();
    expect(screen.getByTestId("turn-footer")).toHaveAttribute("data-live-text", "false");
    answer(runId, "openai:gpt-4.1-mini", "0.001", [5, 5]);
    expect(await screen.findByText("answer from openai:gpt-4.1-mini")).toBeInTheDocument();
    expect(screen.queryByTestId("turn-output-checking")).not.toBeInTheDocument();
  });

  it("withholds live text while the target spec is unknown, then streams once it is known", async () => {
    let resolveVersions: (items: AgentVersion[]) => void = () => undefined;
    vi.mocked(api.versions.list).mockImplementation(
      () => new Promise<AgentVersion[]>((resolve) => { resolveVersions = resolve; }),
    );
    await renderReady();
    await sendMessage("Hi");
    const runId = vi.mocked(startTurn).mock.calls[0][1];
    act(() => {
      handlers.onEvent?.(event(0, "run_started", { input: "Hi" }));
      handlers.onDelta?.({ run_id: runId, model_request: 0, attempt: "a", delta: "early text" });
    });
    expect(await screen.findByTestId("turn-output-checking")).toBeInTheDocument();
    expect(screen.getByTestId("turn-footer")).toHaveAttribute("data-live-text", "false");
    expect(screen.queryByText(/early text/)).not.toBeInTheDocument();
    await act(async () => resolveVersions(versionsByAgent["agent-1"]));
    await waitFor(() => expect(screen.queryByTestId("turn-output-checking")).not.toBeInTheDocument());
    expect(screen.getByTestId("turn-footer")).toHaveAttribute("data-live-text", "true");
  });

  it("does not show the output-check state for an agent without output processors", async () => {
    await renderReady();
    await waitFor(() => expect(api.versions.list).toHaveBeenCalled());
    await sendMessage("Hi");
    await screen.findByTestId("turn-badge");
    expect(screen.queryByTestId("turn-output-checking")).not.toBeInTheDocument();
  });

  it("shows a blocked answer under its message with the fixed message, once", async () => {
    await renderReady();
    await sendMessage("Hi");
    act(() => {
      handlers.onEvent?.(event(0, "run_started", { input: "Hi" }));
      handlers.onEvent?.(event(1, "model_message", { model_request: 0, is_final: true, blocked: true }));
      handlers.onEvent?.(
        event(2, "run_failed", {
          status: "failed",
          error: "the answer was blocked by this agent's output processors",
          error_code: "output_blocked",
        }),
      );
    });
    const blocked = await screen.findByTestId("turn-blocked");
    expect(blocked).toHaveAttribute("data-code", "output_blocked");
    expect(blocked).toHaveTextContent("Answer blocked");
    expect(blocked).toHaveTextContent("The answer was blocked by this agent's output processors.");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    await waitFor(() => expect(composer()).toBeEnabled());
  });

  it("shows a loaded turn's recorded model, tokens, cost and block", async () => {
    mockConversation(
      [
        {
          ...turnRun("run-1", 1, "Hi", "completed"),
          output: "Hey",
          model_id: "test:echo",
          model_overridden: true,
          input_tokens: 120,
          output_tokens: 30,
          cost_usd: "0.0012000000",
        } as ConversationRun,
        { ...turnRun("run-2", 2, "my card", "failed"), error: "the message was blocked by this agent's input processors" },
      ],
      {
        "run-1": answered("Hi", "Hey"),
        "run-2": [
          event(0, "run_started", { input: "[withheld: blocked by the agent's input processors]" }),
          event(1, "run_failed", {
            status: "failed",
            error: "the message was blocked by this agent's input processors",
            error_code: "input_blocked",
          }),
        ],
      },
    );
    window.history.replaceState({}, "", routes.agentChat("agent-3", { conversation: "conv-1" }));
    await renderReady();
    await waitFor(() => expect(screen.getAllByTestId("turn-badge")).toHaveLength(2));
    const [first] = screen.getAllByTestId("turn-badge");
    expect(first).toHaveTextContent("test:echo");
    expect(first).toHaveTextContent("override");
    expect(first).toHaveTextContent("120 in · 30 out tokens");
    expect(first).toHaveTextContent("$0.0012");
    expect(screen.getByTestId("turn-blocked")).toHaveTextContent("Message blocked");
    // A block found on reload is shown, not announced.
    expect(screen.getByTestId("turn-blocked")).not.toHaveAttribute("role");
    expect(screen.getByTestId("turn-blocked")).toHaveTextContent("The message was blocked by this agent's input processors.");
  });

  it("re-posts a recorded draft turn with the options it was recorded with", async () => {
    mockConversation(
      [
        {
          ...turnRun("run-1", 1, "Hello", "queued"),
          version: null,
          draft: true,
          model_id: "test:echo",
          model_overridden: true,
          request_context: { tier: "gold" },
        } as unknown as ConversationRun,
      ],
      {},
    );
    window.history.replaceState({}, "", routes.agentChat("agent-3", { conversation: "conv-1" }));
    await renderReady();
    await waitFor(() =>
      expect(startTurn).toHaveBeenCalledWith("conv-1", "run-1", "Hello", {
        version: "draft",
        model: { provider: "test", name: "echo" },
        request_context: { tier: "gold" },
      }),
    );
  });

  it("preselects the context preset saved on the Request Context page", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (agentId, version) => tierInfo(agentId, version));
    const { saveContext } = await import("@/lib/requestContext");
    saveContext("agent-1", { tier: "gold" }, null);
    window.history.replaceState({}, "", routes.agentChat("agent-1"));
    await renderReady();
    fireEvent.click(await screen.findByRole("button", { name: /1 of 1 set/ }));
    expect(screen.getByText("Using the preset saved for this agent.")).toBeInTheDocument();
    await sendMessage("Hi");
    expect(lastOptions()?.request_context).toEqual({ tier: "gold" });
  });

  it("lets the saved preset win over the tab's remembered context with context=preset, then drops the flag", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (agentId, version) => tierInfo(agentId, version));
    const { saveContext } = await import("@/lib/requestContext");
    saveContext("agent-1", { tier: "gold" }, "draft");
    const key = "eap.playground.context.v1:anon:agent-1";
    window.sessionStorage.setItem(key, JSON.stringify({ value: { tier: "silver" } }));
    window.history.replaceState({}, "", routes.agentChat("agent-1", { version: "draft", context: "preset" }));
    await renderReady();
    fireEvent.click(await screen.findByRole("button", { name: /1 of 1 set/ }));
    expect(screen.getByText("Using the preset saved for this agent.")).toBeInTheDocument();
    expect(window.sessionStorage.getItem(key)).toBeNull();
    const params = new URLSearchParams(window.location.search);
    expect(params.get("context")).toBeNull();
    expect(params.get("version")).toBe("draft");
    await sendMessage("Hi");
    expect(lastOptions()?.request_context).toEqual({ tier: "gold" });
    expect(lastOptions()?.version).toBe("draft");
  });

  it("keeps the tab's remembered context over the preset without the flag", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (agentId, version) => tierInfo(agentId, version));
    const { saveContext } = await import("@/lib/requestContext");
    saveContext("agent-1", { tier: "gold" }, null);
    window.sessionStorage.setItem("eap.playground.context.v1:anon:agent-1", JSON.stringify({ value: { tier: "silver" } }));
    window.history.replaceState({}, "", routes.agentChat("agent-1"));
    await renderReady();
    await screen.findByRole("button", { name: /1 of 1 set/ });
    await sendMessage("Hi");
    expect(lastOptions()?.request_context).toEqual({ tier: "silver" });
  });

  it("sends no context to a version without context fields, even when a value is remembered", async () => {
    window.sessionStorage.setItem(`eap.playground.context.v1:anon:agent-1`, JSON.stringify({ value: { stale: 1 } }));
    await renderReady();
    await waitFor(() => expect(getRequestContext).toHaveBeenCalled());
    await screen.findByRole("button", { name: /No context fields/ });
    await sendMessage("Hi");
    expect(lastOptions()?.request_context).toBeNull();
  });
});
