import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlaygroundPage from "./page";
import { api } from "@/lib/api";
import {
  createConversation,
  getConversation,
  startTurn,
  type ConversationRun,
} from "@/lib/conversations";
import {
  getRun,
  listEvents,
  startRun,
  subscribeRun,
  type RunEvent,
  type SubscribeHandlers,
} from "@/lib/runs";

vi.mock("@/lib/api", () => ({
  api: { agents: { list: vi.fn() } },
}));

vi.mock("@/lib/conversations", () => ({
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  startTurn: vi.fn(),
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

const run = (id: string, display_status = "running") => ({
  id,
  status: display_status,
  display_status: display_status as "running" | "recovering" | "completed",
  agent_id: "agent-1",
  version: 1,
});

const event = (
  seq: number,
  type: RunEvent["type"],
  data: unknown,
): RunEvent => ({ seq, type, data, ts: "2026-11-12T00:00:00Z" });

let handlers: SubscribeHandlers;
const unsubscribe = vi.fn();

function composer() {
  return screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
}

async function renderReady() {
  const view = render(<PlaygroundPage />);
  await screen.findByRole("option", { name: "Support assistant" });
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
  conversation_id: "conv-1",
  turn,
});

function mockConversation(runs: ConversationRun[], events: Record<string, RunEvent[]>) {
  vi.mocked(getConversation).mockResolvedValue({
    id: "conv-1",
    agent_id: "agent-3",
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
  window.history.replaceState({}, "", "/playground");
  vi.clearAllMocks();
  vi.mocked(api.agents.list).mockResolvedValue(agents);
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
    expect([agentId, title]).toEqual(["agent-1", "Hello"]);
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
    expect(new URLSearchParams(window.location.search).get("conversation")).toBe(conversationId);
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
      `/runs/${secondRun}`,
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
    window.history.replaceState({}, "", "/playground?conversation=conv-1");
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
    expect(screen.getByRole("combobox", { name: "Agent" })).toHaveValue("agent-3");
    expect(screen.getByRole("link", { name: "View run" })).toHaveAttribute("href", "/runs/run-2");

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
    window.history.replaceState({}, "", "/playground?conversation=conv-1");
    await renderReady();

    await waitFor(() => expect(startTurn).toHaveBeenCalledWith("conv-1", "run-1", "Hello"));
    await waitFor(() =>
      expect(subscribeRun).toHaveBeenCalledWith("run-1", expect.any(Object), { initialLastSeq: -1 }),
    );
    expect(vi.mocked(startTurn).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(subscribeRun).mock.invocationCallOrder[0],
    );
    expect(screen.getByText("Hello")).toBeInTheDocument();
  });

  it("does not subscribe when every turn of the loaded conversation is finished", async () => {
    mockConversation(
      [{ ...turnRun("run-1", 1, "Hi", "completed"), output: "Hey" }],
      { "run-1": answered("Hi", "Hey") },
    );
    window.history.replaceState({}, "", "/playground?conversation=conv-1");
    await renderReady();

    expect(await screen.findByText("Hey")).toBeInTheDocument();
    expect(subscribeRun).not.toHaveBeenCalled();
    await waitFor(() => expect(composer()).toBeEnabled());
  });

  it("starts a new conversation and clears the URL when switching agents or pressing New conversation", async () => {
    const view = await renderReady();
    await sendMessage();
    expect(new URLSearchParams(window.location.search).get("conversation")).not.toBeNull();

    fireEvent.change(screen.getByRole("combobox", { name: "Agent" }), {
      target: { value: "agent-3" },
    });
    expect(unsubscribe).toHaveBeenCalledTimes(1);
    expect(new URLSearchParams(window.location.search).get("conversation")).toBeNull();
    expect(screen.queryByText("Please create a ticket")).not.toBeInTheDocument();

    await sendMessage("Fresh start");
    expect(createConversation).toHaveBeenCalledTimes(2);
    expect(vi.mocked(createConversation).mock.calls[1][0]).toBe("agent-3");
    act(() => {
      handlers.onEvent?.(event(2, "run_completed", {}));
    });
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    expect(screen.queryByText("Fresh start")).not.toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get("conversation")).toBeNull();

    view.unmount();
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
    window.history.replaceState({}, "", "/playground?conversation=conv-1");

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
    window.history.replaceState({}, "", "/playground?conversation=conv-1");

    await renderReady();

    const ticket = await screen.findByText("TCK-42");
    expect(ticket.tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*TCK-42\*\*/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "details" })).toHaveAttribute(
      "href",
      "https://example.com/tickets/42",
    );
  });
});
