// RM.18 in the Chat tab: the "Act as user" chip (resource_id for NEW conversations only), the open
// conversation's user, and "Delete conversation" from a thread row's menu.
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Agent, type AgentSpec, type AgentVersion } from "@/lib/api";
import { createConversation, deleteConversation, getConversation, listConversations } from "@/lib/conversations";
import { fetchModels } from "@/lib/metrics";
import { getRequestContext } from "@/lib/requestContext";
import { listEvents, subscribeRun } from "@/lib/runs";
import { TurnStartError, startTurnWithOptions } from "@/components/chat/turnApi";
import { routes } from "@/lib/routes";
import { ChatTab, discardEmptyConversation } from "./ChatTab";

vi.mock("next/navigation", () => ({
  usePathname: () => null,
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
vi.mock("@/lib/metrics", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/metrics")>();
  return { ...actual, fetchModels: vi.fn() };
});
vi.mock("@/components/chat/turnApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/chat/turnApi")>();
  return { ...actual, startTurnWithOptions: vi.fn() };
});
vi.mock("@/lib/requestContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/requestContext")>();
  return { ...actual, getRequestContext: vi.fn(), validateRequestContext: vi.fn() };
});
vi.mock("@/lib/runs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/runs")>();
  return { ...actual, listEvents: vi.fn(), subscribeRun: vi.fn() };
});

const specOf = (extra: object = {}): AgentSpec =>
  ({ spec_version: 1, name: "Support bot", instructions: "Help.", model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [], ...extra }) as AgentSpec;
const MEMORY = { memory: { working: { scope: "resource" } } };

function setAgent(spec: AgentSpec) {
  const agent: Agent = { id: "a1", name: "Support bot", draft_spec: spec, latest_version: 1 };
  const versions: AgentVersion[] = [{ agent_id: "a1", version: 1, spec, spec_hash: "h1", created_at: "2026-11-14T00:00:00Z", created_by: "dev" }];
  vi.mocked(api.agents.get).mockResolvedValue(agent);
  vi.mocked(api.versions.list).mockResolvedValue(versions);
}

const conversation = (id: string, resourceId: string | null, extra: object = {}) => ({
  id, agent_id: "a1", resource_id: resourceId, title: `Chat ${id}`, turn_count: 1, last_turn_run_id: null, last_turn_status: "completed",
  last_turn_display_status: "completed", created_at: "2026-11-14T00:00:00Z", updated_at: "2026-11-14T00:00:00Z", ...extra,
});

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", routes.agentChat("a1"));
  setAgent(specOf(MEMORY));
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null } as never);
  vi.mocked(fetchModels).mockResolvedValue([]);
  vi.mocked(getRequestContext).mockResolvedValue({ agent_id: "a1", version: 1, draft: false, fields: [], schema: { type: "object", properties: {} } } as never);
  vi.mocked(subscribeRun).mockReturnValue(() => undefined);
  vi.mocked(listEvents).mockResolvedValue([]);
  vi.mocked(createConversation).mockImplementation(async (agentId, title, id, resourceId) => ({
    id: id ?? "c1", agent_id: agentId, title, resource_id: resourceId ?? "dev-user", turn_count: 0, created_at: "", updated_at: "",
  }) as never);
  vi.mocked(startTurnWithOptions).mockImplementation(async (conversationId, runId) => ({
    run_id: runId, status: "queued", display_status: "queued", agent_id: "a1", version: 1, draft: false,
    model_id: "openai:gpt-4.1-mini", model_overridden: false, conversation_id: conversationId, turn: 1,
  }) as never);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function send(text: string) {
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
}

async function actAs(id: string) {
  fireEvent.click(await screen.findByTestId("composer-chip-act-as"));
  fireEvent.change(await screen.findByTestId("act-as-input"), { target: { value: id } });
  fireEvent.click(screen.getByTestId("act-as-apply"));
}

describe("Act as user", () => {
  it("is not offered for an agent without memory", async () => {
    setAgent(specOf());
    render(<ChatTab agentId="a1" />);
    await screen.findByTestId("composer-chip-model");
    await waitFor(() => expect(api.versions.list).toHaveBeenCalled());
    expect(screen.queryByTestId("composer-chip-act-as")).toBeNull();
  });

  it("sets resource_id on a new conversation only, then shows that conversation's user", async () => {
    render(<ChatTab agentId="a1" />);
    await actAs("alice");
    await waitFor(() => expect(screen.getByTestId("composer-chip-act-as")).toHaveAccessibleName("Act as user: alice"));
    await send("Hi");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createConversation).mock.calls[0][3]).toBe("alice");
    // The turn start carries no resource_id: it is the conversation's, set once.
    expect(Object.keys(vi.mocked(startTurnWithOptions).mock.calls[0][3])).not.toContain("resource_id");
    // The open conversation keeps its user; another choice waits for the next new conversation.
    await actAs("bob");
    expect(screen.getByTestId("composer-chip-act-as")).toHaveAccessibleName("Act as user: alice (next: bob)");
    expect(createConversation).toHaveBeenCalledTimes(1);
  });

  it("acting as yourself sends no resource_id", async () => {
    render(<ChatTab agentId="a1" />);
    await screen.findByTestId("composer-chip-act-as");
    await send("Hi");
    await waitFor(() => expect(createConversation).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createConversation).mock.calls[0][3]).toBeNull();
  });

  it("an opened conversation shows its own user", async () => {
    vi.mocked(getConversation).mockResolvedValue({ ...conversation("c7", "bob@example.com"), runs: [] } as never);
    render(<ChatTab agentId="a1" conversationId="c7" />);
    await waitFor(() => expect(screen.getByTestId("composer-chip-act-as")).toHaveAccessibleName("Act as user: bob@example.com"));
  });

  it("is remembered for the agent in this tab", async () => {
    const first = render(<ChatTab agentId="a1" />);
    await actAs("carol");
    first.unmount();
    render(<ChatTab agentId="a1" />);
    await waitFor(() => expect(screen.getByTestId("composer-chip-act-as")).toHaveAccessibleName("Act as user: carol"));
  });
});

describe("a refused first send leaves no empty conversation behind", () => {
  const refuseOnce = () =>
    vi.mocked(startTurnWithOptions).mockRejectedValueOnce(new TurnStartError(422, [{ loc: ["body", "model"], msg: "model is not allowed", type: "value_error" }]));

  it("the same user reuses the empty conversation", async () => {
    refuseOnce();
    render(<ChatTab agentId="a1" />);
    await actAs("alice");
    await send("one");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(1));
    await send("again");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(2));
    expect(createConversation).toHaveBeenCalledTimes(1);
    expect(deleteConversation).not.toHaveBeenCalled();
    expect(vi.mocked(startTurnWithOptions).mock.calls[1][0]).toBe(vi.mocked(startTurnWithOptions).mock.calls[0][0]);
  });

  /** The refused first send's conversation, as the API has it: no turns. */
  const emptyOnServer = () =>
    vi.mocked(getConversation).mockImplementation(async (id) => ({ ...conversation(id, "alice", { turn_count: 0 }), runs: [] }) as never);

  /** A first send refused after its conversation was created; resolves to that conversation's id. */
  async function refusedFirstSend(): Promise<string> {
    refuseOnce();
    await screen.findByTestId("composer-chip-act-as");
    await send("one");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("one"));
    return vi.mocked(createConversation).mock.calls[0][2] as string;
  }

  it("opening another conversation deletes the empty one this tab created", async () => {
    emptyOnServer();
    vi.mocked(deleteConversation).mockResolvedValue(undefined);
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c7", "bob")], next_before: null } as never);
    render(<ChatTab agentId="a1" />);
    const empty = await refusedFirstSend();
    fireEvent.click(await screen.findByText("Chat c7"));
    await waitFor(() => expect(deleteConversation).toHaveBeenCalledWith(empty));
    expect(getConversation).toHaveBeenCalledWith(empty);
    expect(deleteConversation).toHaveBeenCalledTimes(1);
  });

  it("New conversation deletes it too; a failed delete shows nothing", async () => {
    emptyOnServer();
    vi.mocked(deleteConversation).mockRejectedValue(Object.assign(new Error("boom"), { status: 500 }));
    render(<ChatTab agentId="a1" />);
    const empty = await refusedFirstSend();
    fireEvent.click(screen.getByRole("button", { name: /New conversation/ }));
    await waitFor(() => expect(deleteConversation).toHaveBeenCalledWith(empty));
    // The failure is not shown anywhere (the alert left is the refused send's own model error).
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.body).not.toHaveTextContent("boom");
    expect(screen.queryByText(/delete/i)).toBeNull();
  });

  it("switching threads while the first start is in flight deletes nothing, and the turn still starts", async () => {
    emptyOnServer();
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c7", "bob")], next_before: null } as never);
    let finish: (value: unknown) => void = () => {};
    vi.mocked(startTurnWithOptions).mockImplementationOnce(
      (conversationId, runId) =>
        new Promise((resolve) => {
          finish = () => resolve({ run_id: runId, status: "queued", display_status: "queued", agent_id: "a1", version: 1, conversation_id: conversationId, turn: 1 });
        }) as never,
    );
    render(<ChatTab agentId="a1" />);
    await screen.findByTestId("composer-chip-act-as");
    await send("one");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(1));
    const starting = vi.mocked(createConversation).mock.calls[0][2] as string;
    fireEvent.click(await screen.findByText("Chat c7"));
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith("c7"));
    expect(getConversation).not.toHaveBeenCalledWith(starting);
    expect(deleteConversation).not.toHaveBeenCalled();
    await act(async () => {
      finish(undefined);
      await Promise.resolve();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(deleteConversation).not.toHaveBeenCalled();
    expect(vi.mocked(startTurnWithOptions).mock.calls[0][0]).toBe(starting);
  });

  it("a start that fails after the user switched threads is discarded (it is still empty)", async () => {
    emptyOnServer();
    vi.mocked(deleteConversation).mockResolvedValue(undefined);
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c7", "bob")], next_before: null } as never);
    let fail: (error: unknown) => void = () => {};
    vi.mocked(startTurnWithOptions).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }) as never);
    render(<ChatTab agentId="a1" />);
    await screen.findByTestId("composer-chip-act-as");
    await send("one");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(1));
    const starting = vi.mocked(createConversation).mock.calls[0][2] as string;
    fireEvent.click(await screen.findByText("Chat c7"));
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith("c7"));
    expect(deleteConversation).not.toHaveBeenCalled();
    await act(async () => {
      fail(new TurnStartError(503, "worker unavailable"));
    });
    await waitFor(() => expect(deleteConversation).toHaveBeenCalledWith(starting));
  });

  it("discardEmptyConversation: deletes only at zero turns, and a 404 or a failed read is quiet", async () => {
    vi.mocked(getConversation).mockResolvedValueOnce({ ...conversation("e1", null, { turn_count: 0 }), runs: [] } as never);
    vi.mocked(deleteConversation).mockRejectedValueOnce(Object.assign(new Error("Conversation not found"), { status: 404 }));
    await expect(discardEmptyConversation("e1")).resolves.toBeUndefined();
    expect(deleteConversation).toHaveBeenCalledWith("e1");
    vi.mocked(getConversation).mockRejectedValueOnce(Object.assign(new Error("Conversation not found"), { status: 404 }));
    await expect(discardEmptyConversation("e2")).resolves.toBeUndefined();
    expect(deleteConversation).toHaveBeenCalledTimes(1);
  });

  it("a conversation that got a turn after all (the start failed in transit) is kept", async () => {
    vi.mocked(getConversation).mockImplementation(async (id) => ({ ...conversation(id, "alice", { turn_count: 1 }), runs: [] }) as never);
    render(<ChatTab agentId="a1" />);
    const pendingId = await refusedFirstSend();
    fireEvent.click(screen.getByRole("button", { name: /New conversation/ }));
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith(pendingId));
    expect(deleteConversation).not.toHaveBeenCalled();
  });

  it("another user: the empty one is deleted before the new one is created", async () => {
    refuseOnce();
    emptyOnServer();
    vi.mocked(deleteConversation).mockResolvedValue(undefined);
    render(<ChatTab agentId="a1" />);
    await actAs("alice");
    await send("one");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(1));
    const empty = vi.mocked(createConversation).mock.calls[0][2];
    await actAs("bob");
    await send("again");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(2));
    expect(deleteConversation).toHaveBeenCalledWith(empty);
    expect(vi.mocked(deleteConversation).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(createConversation).mock.invocationCallOrder[1]);
    expect(vi.mocked(createConversation).mock.calls[1][3]).toBe("bob");
    expect(vi.mocked(startTurnWithOptions).mock.calls[1][0]).not.toBe(empty);
  });
});

describe("Delete conversation", () => {
  it("from the row menu, confirmed: the open conversation is closed and the row goes", async () => {
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c7", "bob")], next_before: null } as never);
    vi.mocked(getConversation).mockResolvedValue({ ...conversation("c7", "bob"), runs: [] } as never);
    vi.mocked(deleteConversation).mockResolvedValue(undefined);
    render(<ChatTab agentId="a1" conversationId="c7" />);
    const trigger = await screen.findByTestId("conversation-actions");
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(await screen.findByTestId("conversation-delete"));
    const dialog = await screen.findByRole("dialog", { name: "Delete this conversation?" });
    expect(dialog).toHaveTextContent("runs stay in Traces");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete conversation" }));
    await waitFor(() => expect(deleteConversation).toHaveBeenCalledWith("c7"));
    await waitFor(() => expect(screen.queryByTestId("conversation-actions")).toBeNull());
    await waitFor(() => expect(window.location.pathname).toBe("/agents/a1/chat"));
  });

  it("a 409 (a turn is running) stays in the dialog with the API's message", async () => {
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c7", "bob")], next_before: null } as never);
    vi.mocked(deleteConversation).mockRejectedValue(Object.assign(new Error("The latest turn (run r1) is still running; nothing was deleted. Retry when it finishes"), { status: 409 }));
    render(<ChatTab agentId="a1" />);
    const trigger = await screen.findByTestId("conversation-actions");
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(await screen.findByTestId("conversation-delete"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete conversation" }));
    expect(await within(dialog).findByTestId("confirm-dialog-error")).toHaveTextContent("nothing was deleted");
    expect(screen.getByTestId("conversation-actions")).toBeInTheDocument();
  });
});
