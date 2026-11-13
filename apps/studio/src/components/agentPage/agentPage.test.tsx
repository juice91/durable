/**
 * SL.6 implementer tests: the agent page shell's wiring (layout, tabs, panes, Config, redirects).
 * The re-pointed P0 playground and editor suites cover the chat and editor bodies.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const nav = vi.hoisted(() => ({ path: "/agents/a1/chat", params: { id: "a1" } as Record<string, string>, push: vi.fn(), replace: vi.fn() }));
const redirectMock = vi.hoisted(() => vi.fn());

vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => nav.params,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  redirect: (url: string) => redirectMock(url),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { get: vi.fn(), list: vi.fn() }, versions: { list: vi.fn() } } };
});
vi.mock("@/lib/conversations", () => ({
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  listConversations: vi.fn(),
  renameConversation: vi.fn(),
}));
vi.mock("@/components/playground/turnApi", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/components/playground/turnApi")>();
  return { ...original, startTurnWithOptions: vi.fn() };
});
vi.mock("@/lib/runs", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/runs")>();
  return { ...original, listEvents: vi.fn(async () => []), subscribeRun: vi.fn(() => () => undefined) };
});
vi.mock("@/lib/metrics", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/metrics")>();
  return { ...original, fetchModels: vi.fn(async () => []) };
});
vi.mock("@/lib/requestContext", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/requestContext")>();
  return { ...original, getRequestContext: vi.fn(), validateRequestContext: vi.fn(async (_a: string, v: unknown) => v) };
});
vi.mock("@/lib/traces", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/traces")>();
  return { ...original, listTraceRuns: vi.fn() };
});

import { api, ApiError, type Agent, type AgentSpec } from "@/lib/api";
import { createConversation, getConversation, listConversations } from "@/lib/conversations";
import { startTurnWithOptions } from "@/components/playground/turnApi";
import { getRequestContext } from "@/lib/requestContext";
import { listTraceRuns } from "@/lib/traces";
import { routes } from "@/lib/routes";
import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";
import { AgentLayout } from "./AgentLayout";
import { ChatTab } from "./ChatTab";
import { TracesTab } from "./TracesTab";
import { playgroundClientTarget } from "./PlaygroundRedirect";
import { AgentsListUI } from "@/components/agents/AgentsList";
import { readConfigOpen } from "./agentPageModel";
import { AGENTS_VIEW_KEY, LAST_AGENT_KEY, agentLayoutKey } from "./agentPageModel";

const specOf = (name: string, extra: object = {}): AgentSpec =>
  ({ spec_version: 1, name, instructions: "Help with {{context.plan}}", model: { provider: "openai", name: "gpt-4.1-mini" }, tools: ["create_ticket"], ...extra }) as AgentSpec;

const AGENTS: Agent[] = [
  { id: "a1", name: "Support bot", draft_spec: specOf("Support bot", { instructions: "Draft instructions" }), latest_version: 1 },
  { id: "a2", name: "Billing bot", draft_spec: specOf("Billing bot"), latest_version: null },
];

let media: MatchMediaControl | undefined;

beforeEach(() => {
  window.localStorage.clear();
  window.history.replaceState({}, "", routes.agentChat("a1"));
  nav.path = routes.agentChat("a1");
  nav.params = { id: "a1" };
  nav.push.mockReset();
  nav.replace.mockReset();
  redirectMock.mockReset();
  vi.mocked(api.agents.get).mockImplementation(async (id: string) => {
    const agent = AGENTS.find((a) => a.id === id);
    if (!agent) throw new ApiError(404, "not found");
    return agent;
  });
  vi.mocked(api.agents.list).mockResolvedValue(AGENTS);
  vi.mocked(api.versions.list).mockImplementation(async (id: string) =>
    id === "a1" ? [{ agent_id: "a1", version: 1, spec: specOf("Support bot"), spec_hash: "h", created_at: "2026-11-14T00:00:00Z", created_by: "dev" }] : [],
  );
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null });
  vi.mocked(getRequestContext).mockImplementation(async (agentId, version) => ({
    agent_id: agentId,
    version: typeof version === "number" ? version : null,
    draft: version === "draft",
    fields: [{ name: "plan", type: "string" as const, required: false, description: "", max_length: null, values: [] }],
    schema: { type: "object" as const, properties: { plan: { type: "string" as const } } },
  }));
});

afterEach(() => {
  cleanup();
  media?.restore();
  media = undefined;
});

function renderChat(wide = true) {
  media = mockMatchMedia(wide);
  return render(
    <FrameHarness>
      <AgentLayout agentId="a1">
        <ChatTab agentId="a1" />
      </AgentLayout>
    </FrameHarness>,
  );
}

const main = () => screen.getByRole("main");

describe("agent page shell (wide)", () => {
  it("shows the agent as the h1 with the switcher, the three tabs, and Config open on the Chat tab", async () => {
    renderChat();
    await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot"));
    const tabs = within(main()).getByRole("navigation", { name: "Agent sections" });
    expect(within(tabs).getByRole("link", { name: "Chat" })).toHaveAttribute("href", routes.agentChat("a1"));
    expect(within(tabs).getByRole("link", { name: "Chat" })).toHaveAttribute("aria-current", "page");
    expect(within(tabs).getByRole("link", { name: "Traces" })).toHaveAttribute("href", routes.agentTraces("a1"));
    expect(within(tabs).getByRole("link", { name: "Editor" })).toHaveAttribute("href", routes.agentEditor("a1"));
    const config = await screen.findByRole("complementary", { name: "Config" });
    expect(main()).not.toContainElement(config);
    // Latest published (v1), not the draft.
    await waitFor(() => expect(within(config).getByTestId("config-version")).toHaveTextContent("v1 (latest)"));
    expect(within(config).getByTestId("config-instructions")).toHaveTextContent("Help with {{context.plan}}");
    expect(within(config).getByTestId("config-instructions").querySelector("mark")).toHaveTextContent("{{context.plan}}");
    expect(within(config).getByRole("link", { name: "Edit tools" })).toHaveAttribute("href", routes.agentEditor("a1", { step: "tools" }));
    expect(screen.getByTestId("memory-card")).toBeInTheDocument();
    expect(window.localStorage.getItem(LAST_AGENT_KEY)).toBe("a1");
  });

  it("the switcher goes to the same tab of another agent", async () => {
    nav.path = routes.agentTraces("a1");
    media = mockMatchMedia(true);
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [], next_before: null });
    render(
      <FrameHarness>
        <AgentLayout agentId="a1">
          <TracesTab agentId="a1" />
        </AgentLayout>
      </FrameHarness>,
    );
    fireEvent.click(await screen.findByRole("combobox", { name: "Switch agent" }));
    fireEvent.click(await screen.findByRole("option", { name: "Billing bot" }));
    expect(nav.push).toHaveBeenCalledWith(routes.agentTraces("a2"));
    expect(listTraceRuns).toHaveBeenCalledWith(expect.objectContaining({ agent_id: "a1" }), expect.anything());
  });

  it("`]` toggles Config and `{` the conversations pane; typing in the composer does neither", async () => {
    renderChat();
    await screen.findByRole("complementary", { name: "Config" });
    const threadsToggle = screen.getByRole("button", { name: "Conversations" });
    expect(threadsToggle).toHaveAttribute("aria-pressed", "true");
    fireEvent.keyDown(document.body, { key: "]" });
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();
    fireEvent.keyDown(document.body, { key: "{" });
    expect(threadsToggle).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByRole("navigation", { name: "Conversations" })).toBeNull();
    // The collapse is saved, so a reload keeps it.
    await waitFor(() => expect(JSON.parse(window.localStorage.getItem(agentLayoutKey("a1")) ?? "{}").threads).toBe(0));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "]" });
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();
    fireEvent.keyDown(document.body, { key: "]" });
    fireEvent.keyDown(document.body, { key: "{" });
    expect(await screen.findByRole("complementary", { name: "Config" })).toBeInTheDocument();
    expect(threadsToggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("navigation", { name: "Conversations" })).toBeInTheDocument();
  });

  it("restores the pane sizes saved for this agent, and starts collapsed when they say so", async () => {
    // jsdom has no layout, so a drag cannot run here; ResizableGroup saves on user resizes (kit test).
    window.localStorage.setItem(agentLayoutKey("a1"), JSON.stringify({ threads: 30, chat: 70 }));
    window.localStorage.setItem(agentLayoutKey("a2"), JSON.stringify({ threads: 0, chat: 100 }));
    renderChat();
    await screen.findByRole("separator", { name: "Resize conversations panel" });
    expect(document.querySelector<HTMLElement>("[data-panel][id='threads']")?.style.flexGrow).toBe("30");
    expect(screen.getByRole("button", { name: "Conversations" })).toHaveAttribute("aria-pressed", "true");
    cleanup();
    media?.restore();
    media = mockMatchMedia(true);
    render(
      <FrameHarness>
        <AgentLayout agentId="a2">
          <ChatTab agentId="a2" />
        </AgentLayout>
      </FrameHarness>,
    );
    await screen.findByRole("separator", { name: "Resize conversations panel" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Conversations" })).toHaveAttribute("aria-pressed", "false"));
    expect(screen.queryByRole("navigation", { name: "Conversations" })).toBeNull();
  });

  it("the request context panel shows inside Config, never in the chat column", async () => {
    renderChat();
    const toggle = await screen.findByRole("button", { name: /Request context/ });
    await waitFor(() => expect(toggle).toBeEnabled());
    fireEvent.click(toggle);
    const panel = await screen.findByTestId("playground-context-panel");
    expect(screen.getByRole("complementary", { name: "Config" })).toContainElement(panel);
    expect(screen.getByTestId("agent-chat")).not.toContainElement(panel);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  it("a first send puts the new conversation in the path and keeps the options in the query", async () => {
    window.history.replaceState({}, "", routes.agentChat("a1", { version: 1 }));
    vi.mocked(createConversation).mockImplementation(async (agentId, title, id) => ({
      id: id ?? "c-new",
      agent_id: agentId,
      title,
      turn_count: 0,
      created_at: "2026-11-14T00:00:00Z",
      updated_at: "2026-11-14T00:00:00Z",
    }));
    vi.mocked(startTurnWithOptions).mockImplementation(async (conversationId, runId) => ({
      run_id: runId,
      status: "queued",
      display_status: "queued",
      agent_id: "a1",
      version: 1,
      conversation_id: conversationId,
      turn: 1,
    }));
    renderChat();
    const composer = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());
    fireEvent.change(composer, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalled());
    const conversationId = vi.mocked(createConversation).mock.calls[0][2]!;
    expect(`${window.location.pathname}${window.location.search}`).toBe(routes.agentChat("a1", { conversation: conversationId, version: 1 }));
  });

  it("an old link to another agent's conversation says so and links to that agent's chat", async () => {
    vi.mocked(getConversation).mockResolvedValue({
      id: "c9",
      agent_id: "a2",
      title: "Elsewhere",
      turn_count: 0,
      created_at: "2026-11-14T00:00:00Z",
      updated_at: "2026-11-14T00:00:00Z",
      runs: [],
    });
    media = mockMatchMedia(true);
    render(
      <FrameHarness>
        <AgentLayout agentId="a1">
          <ChatTab agentId="a1" conversationId="c9" />
        </AgentLayout>
      </FrameHarness>,
    );
    const notice = await screen.findByTestId("chat-other-agent");
    expect(within(notice).getByRole("link")).toHaveAttribute("href", routes.agentChat("a2", { conversation: "c9" }));
    expect(window.location.pathname).toBe(routes.agentChat("a1"));
  });

  it("shows the load error instead of the tabs for an unknown agent", async () => {
    media = mockMatchMedia(true);
    render(
      <FrameHarness>
        <AgentLayout agentId="nope">
          <p>tab body</p>
        </AgentLayout>
      </FrameHarness>,
    );
    expect(await screen.findByTestId("agent-load-error")).toHaveTextContent("Agent not found");
    expect(screen.queryByText("tab body")).toBeNull();
  });
});

describe("review fixes: the Chat tab follows the address, Config opens on entering Chat", () => {
  function mockTurns() {
    vi.mocked(createConversation).mockClear();
    vi.mocked(startTurnWithOptions).mockClear();
    vi.mocked(createConversation).mockImplementation(async (agentId, title, id) => ({
      id: id ?? "c-new",
      agent_id: agentId,
      title,
      turn_count: 0,
      created_at: "2026-11-14T00:00:00Z",
      updated_at: "2026-11-14T00:00:00Z",
    }));
    vi.mocked(startTurnWithOptions).mockImplementation(async (conversationId, runId) => ({
      run_id: runId,
      status: "queued",
      display_status: "queued",
      agent_id: "a1",
      version: 1,
      conversation_id: conversationId,
      turn: 1,
    }));
  }

  function tree(tab: React.ReactNode) {
    return (
      <FrameHarness>
        <AgentLayout agentId="a1">{tab}</AgentLayout>
      </FrameHarness>
    );
  }

  async function sendFirst() {
    const composer = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());
    fireEvent.change(composer, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalled());
    return vi.mocked(createConversation).mock.calls[0][2]!;
  }

  it("the Chat tab links to the open conversation with its options, also from the Traces tab", async () => {
    mockTurns();
    window.history.replaceState({}, "", routes.agentChat("a1", { version: 1 }));
    media = mockMatchMedia(true);
    const view = render(tree(<ChatTab agentId="a1" />));
    const conversation = await sendFirst();
    const expected = routes.agentChat("a1", { conversation, version: 1 });
    await waitFor(() => expect(screen.getByTestId("agent-tab-chat")).toHaveAttribute("href", expected));

    vi.mocked(listTraceRuns).mockResolvedValue({ items: [], next_before: null });
    nav.path = routes.agentTraces("a1");
    view.rerender(tree(<TracesTab agentId="a1" />));
    await screen.findByTestId("agent-traces-tab");
    expect(screen.getByTestId("agent-tab-chat")).toHaveAttribute("href", expected);
    expect(screen.getByTestId("agent-tab-chat")).not.toHaveAttribute("aria-current");
  });

  it("a navigation to the bare chat resets the conversation; one to another conversation opens it", async () => {
    mockTurns();
    media = mockMatchMedia(true);
    const view = render(tree(<ChatTab agentId="a1" />));
    const conversation = await sendFirst();
    expect(await screen.findByText("Hello")).toBeInTheDocument();
    // Next syncs the pathname to the chat's own replaceState write: nothing changes.
    nav.path = routes.agentChat("a1", { conversation });
    view.rerender(tree(<ChatTab agentId="a1" />));
    expect(screen.getByText("Hello")).toBeInTheDocument();

    // A link to `/agents/a1/chat` while this route stays mounted (no remount): a new conversation.
    nav.path = routes.agentChat("a1");
    window.history.replaceState({}, "", routes.agentChat("a1"));
    view.rerender(tree(<ChatTab agentId="a1" />));
    await waitFor(() => expect(screen.queryByText("Hello")).toBeNull());
    expect(window.location.pathname).toBe(routes.agentChat("a1"));

    // A link to another of this agent's conversations: it loads.
    vi.mocked(getConversation).mockResolvedValue({
      id: "c2", agent_id: "a1", title: "Other", turn_count: 0,
      created_at: "2026-11-14T00:00:00Z", updated_at: "2026-11-14T00:00:00Z", runs: [],
    });
    nav.path = routes.agentChat("a1", { conversation: "c2" });
    window.history.replaceState({}, "", nav.path);
    view.rerender(tree(<ChatTab agentId="a1" />));
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith("c2"));
  });

  it("checks the conversation's agent before reading any of its events", async () => {
    const { listEvents } = await import("@/lib/runs");
    vi.mocked(listEvents).mockClear();
    vi.mocked(getConversation).mockResolvedValue({
      id: "c9", agent_id: "a2", title: null, turn_count: 1,
      created_at: "2026-11-14T00:00:00Z", updated_at: "2026-11-14T00:00:00Z",
      runs: [{ run_id: "r1" } as never],
    });
    media = mockMatchMedia(true);
    render(tree(<ChatTab agentId="a1" conversationId="c9" />));
    await screen.findByTestId("chat-other-agent");
    expect(listEvents).not.toHaveBeenCalled();
  });

  it("entering Chat from another tab opens Config on wide screens, unless the user closed it there", async () => {
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [], next_before: null });
    media = mockMatchMedia(true);
    nav.path = routes.agentTraces("a1");
    const view = render(tree(<TracesTab agentId="a1" />));
    await screen.findByTestId("agent-traces-tab");
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();

    nav.path = routes.agentChat("a1");
    view.rerender(tree(<ChatTab agentId="a1" />));
    expect(await screen.findByRole("complementary", { name: "Config" })).toBeInTheDocument();

    // The user closes it on Chat: remembered for this agent, and leaving and coming back keeps it closed.
    fireEvent.keyDown(document.body, { key: "]" });
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull());
    expect(readConfigOpen("a1")).toBe(false);
    nav.path = routes.agentTraces("a1");
    view.rerender(tree(<TracesTab agentId="a1" />));
    nav.path = routes.agentChat("a1");
    view.rerender(tree(<ChatTab agentId="a1" />));
    await screen.findByRole("textbox", { name: "Message" });
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();
    expect(readConfigOpen("a2")).toBeNull();
  });

  it("below 1024 px entering Chat opens no Sheet", async () => {
    media = mockMatchMedia(false);
    nav.path = routes.agentEditor("a1");
    const view = render(tree(<p>editor</p>));
    await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot"));
    nav.path = routes.agentChat("a1");
    view.rerender(tree(<ChatTab agentId="a1" />));
    await screen.findByRole("textbox", { name: "Message" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("focus moves into the conversations Sheet and back to its trigger on close", async () => {
    renderChat(false);
    const trigger = await screen.findByRole("button", { name: "Conversations" });
    trigger.focus();
    fireEvent.click(trigger);
    const sheet = await screen.findByRole("dialog", { name: "Conversations" });
    await waitFor(() => expect(sheet).toContainElement(document.activeElement as HTMLElement));
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});

describe("agent page shell below 1024 px", () => {
  it("the chat takes the full width; conversations and Config open as Sheets", async () => {
    renderChat(false);
    await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot"));
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();
    expect(screen.queryByRole("separator", { name: "Resize conversations panel" })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Conversations" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Conversations" }));
    const threads = await screen.findByRole("dialog", { name: "Conversations" });
    expect(within(threads).getByRole("navigation", { name: "Conversations" })).toBeInTheDocument();
    fireEvent.click(within(threads).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Config" }));
    const config = await screen.findByRole("dialog", { name: "Config" });
    await waitFor(() => expect(within(config).getByTestId("config-version")).toHaveTextContent("v1 (latest)"));
  });
});

describe("old playground links without an agent", () => {
  it("open the conversation's agent, else the last-used agent, else the first, else the list", async () => {
    vi.mocked(getConversation).mockResolvedValue({ id: "c1", agent_id: "a2", title: null, turn_count: 1, created_at: "", updated_at: "", runs: [] });
    expect(await playgroundClientTarget("?conversation=c1&model=test%3Aecho")).toBe(routes.agentChat("a2", { conversation: "c1", model: "test:echo" }));
    vi.mocked(getConversation).mockRejectedValue(new Error("gone"));
    window.localStorage.setItem(LAST_AGENT_KEY, "a2");
    expect(await playgroundClientTarget("?conversation=c1")).toBe(routes.agentChat("a2"));
    window.localStorage.removeItem(LAST_AGENT_KEY);
    expect(await playgroundClientTarget("?version=draft")).toBe(routes.agentChat("a1", { version: "draft" }));
    vi.mocked(api.agents.list).mockResolvedValue([]);
    expect(await playgroundClientTarget("")).toBe(routes.agents());
    vi.mocked(api.agents.list).mockRejectedValue(new Error("down"));
    expect(await playgroundClientTarget("")).toBe(routes.agents());
  });
});

describe("redirect pages", () => {
  it("/agents/.. (an id no path can carry) redirects to the agents list instead of throwing", async () => {
    const { default: Page } = await import("@/app/agents/[id]/page");
    await Page({ params: Promise.resolve({ id: ".." }), searchParams: Promise.resolve({}) });
    expect(redirectMock).toHaveBeenCalledWith(routes.agents());
  });

  it("/agents/:id redirects to the agent's chat", async () => {
    const { default: Page } = await import("@/app/agents/[id]/page");
    await Page({ params: Promise.resolve({ id: "a1" }), searchParams: Promise.resolve({}) });
    expect(redirectMock).toHaveBeenCalledWith(routes.agentChat("a1"));
  });

  it("/playground with an agent redirects on the server; without one it renders the client redirect", async () => {
    const { default: Page } = await import("@/app/playground/page");
    await Page({ searchParams: Promise.resolve({ agent: "a1", conversation: "c1", model: "test:echo" }) });
    expect(redirectMock).toHaveBeenCalledWith(routes.agentChat("a1", { conversation: "c1", model: "test:echo" }));
    redirectMock.mockReset();
    const element = await Page({ searchParams: Promise.resolve({}) });
    expect(redirectMock).not.toHaveBeenCalled();
    render(element as React.ReactElement);
    expect(screen.getByTestId("playground-redirect")).toBeInTheDocument();
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(routes.agentChat("a1")));
  });
});

describe("agents list", () => {
  it("rows open the agent's chat; the compact view persists", async () => {
    render(<AgentsListUI agents={AGENTS} />);
    const row = screen.getByRole("link", { name: "Support bot" });
    expect(row).toHaveAttribute("href", routes.agentChat("a1"));
    expect(screen.getByText("Draft only")).toBeInTheDocument();
    expect(screen.getByText("Draft instructions")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Compact" }));
    expect(window.localStorage.getItem(AGENTS_VIEW_KEY)).toBe("compact");
    expect(screen.getAllByTestId("agent-card")[0]).toHaveAttribute("href", routes.agentChat("a1"));
    cleanup();
    render(<AgentsListUI agents={AGENTS} />);
    await act(async () => undefined);
    expect(screen.getByTestId("agents-grid")).toBeInTheDocument();
  });
});
