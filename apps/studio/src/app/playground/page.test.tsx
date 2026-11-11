import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlaygroundPage from "./page";
import { api } from "@/lib/api";
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
  const callsBeforeSend = vi.mocked(startRun).mock.calls.length;
  fireEvent.change(composer(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() =>
    expect(startRun).toHaveBeenCalledTimes(callsBeforeSend + 1),
  );
}

beforeEach(() => {
  window.history.replaceState({}, "", "/playground");
  vi.clearAllMocks();
  vi.mocked(api.agents.list).mockResolvedValue(agents);
  vi.mocked(startRun).mockImplementation(async (_agentId, runId) => run(runId));
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
  it("starts a selected agent run with a UUID before subscribing to it", async () => {
    await renderReady();
    await sendMessage("Hello");

    const [, runId, input, version] = vi.mocked(startRun).mock.calls[0];
    expect(runId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect([input, version]).toEqual(["Hello", null]);
    expect(subscribeRun).toHaveBeenCalledWith(runId, expect.any(Object));
    expect(vi.mocked(startRun).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(subscribeRun).mock.invocationCallOrder[0],
    );
  });

  it("links to the API run_id returned when a run starts", async () => {
    vi.mocked(startRun).mockImplementationOnce(async (_agentId, runId) =>
      ({
        run_id: runId,
        status: "queued",
        display_status: "queued",
        agent_id: "agent-1",
        version: 1,
      }) as unknown as Awaited<ReturnType<typeof startRun>>,
    );
    await renderReady();
    await sendMessage();

    const startedRunId = vi.mocked(startRun).mock.calls[0][1];
    expect(await screen.findByRole("link", { name: "View run" })).toHaveAttribute(
      "href",
      `/runs/${startedRunId}`,
    );
  });

  it("keeps the composer disabled until the run completes", async () => {
    await renderReady();
    await sendMessage();

    await waitFor(() => expect(composer()).toBeDisabled());
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

  it("loads and subscribes to the run in the URL without starting another run", async () => {
    window.history.replaceState({}, "", "/playground?run=existing-run");
    await renderReady();

    await waitFor(() =>
      expect(subscribeRun).toHaveBeenCalledWith("existing-run", expect.any(Object)),
    );
    expect(getRun).toHaveBeenCalledWith("existing-run");
    expect(listEvents).toHaveBeenCalledWith("existing-run");
    expect(startRun).not.toHaveBeenCalled();
  });

  it("unsubscribes on unmount and when switching agents", async () => {
    const view = await renderReady();
    await sendMessage();

    fireEvent.change(screen.getByRole("combobox", { name: "Agent" }), {
      target: { value: "agent-3" },
    });
    expect(unsubscribe).toHaveBeenCalledTimes(1);

    view.unmount();
    expect(unsubscribe).toHaveBeenCalledTimes(1);

    vi.mocked(api.agents.list).mockResolvedValue([agents[0]]);
    const secondView = await renderReady();
    await sendMessage("Again");
    secondView.unmount();
    expect(unsubscribe).toHaveBeenCalledTimes(2);
  });

  it("renders a tool call name and result through assistant-ui message parts", async () => {
    vi.mocked(listEvents).mockResolvedValue([
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
    ]);
    window.history.replaceState({}, "", "/playground?run=tool-run");

    await renderReady();

    expect(await screen.findByText("tickets__create")).toBeInTheDocument();
    expect(screen.getByText(/"ticket_id": "T-42"/)).toBeInTheDocument();
  });

  it("renders assistant markdown without exposing raw markup", async () => {
    vi.mocked(listEvents).mockResolvedValue([
      event(0, "run_started", { input: "What is my ticket?" }),
      event(1, "model_message", {
        model_request: 0,
        text: "Created **TCK-42**. See [details](https://example.com/tickets/42).",
      }),
    ]);
    window.history.replaceState({}, "", "/playground?run=markdown-run");

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
