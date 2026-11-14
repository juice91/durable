/**
 * SL.6 acceptance: the agent page shell end to end (redirects, tabs and switcher, panes, Config,
 * Copy link, Traces, Editor save, agents list, axe). Complements agentPage.test.tsx (wiring) and the
 * re-pointed playground suites (chat body); nothing here repeats those.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const nav = vi.hoisted(() => ({ path: "/agents/a1/chat", push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => ({}),
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  // Like Next: redirect() throws; the test reads the target off the error.
  redirect: (url: string) => {
    throw Object.assign(new Error("NEXT_REDIRECT"), { redirectTo: url });
  },
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { get: vi.fn(), list: vi.fn(), update: vi.fn() }, versions: { list: vi.fn(), create: vi.fn() } } };
});
vi.mock("@/lib/conversations", () => ({
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  listConversations: vi.fn(),
  renameConversation: vi.fn(),
}));
vi.mock("@/components/chat/turnApi", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/components/chat/turnApi")>();
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

import { api, type Agent, type AgentSpec, type AgentVersion } from "@/lib/api";
import { getConversation, listConversations } from "@/lib/conversations";
import { getRequestContext } from "@/lib/requestContext";
import { EMPTY_FILTERS, listTraceRuns, traceFiltersToParams, type TraceRun } from "@/lib/traces";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { FrameHarness, mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH, type MatchMediaControl } from "@/components/frame/frameTesting";
import { AgentLayout } from "./AgentLayout";
import { ChatTab } from "./ChatTab";
import { EditorTab } from "./EditorTab";
import { TracesTab } from "./TracesTab";
import { playgroundClientTarget } from "./PlaygroundRedirect";
import { LAST_AGENT_KEY, agentLayoutKey } from "./agentPageModel";

const MODEL = { provider: "openai", name: "gpt-4.1-mini" };
const RICH = {
  tools: ["create_ticket", "lookup_order"],
  mcp_tools: [{ server: "crm", tool: "find_customer" }],
  input_processors: [{ type: "pii_redactor", config: {} }],
  output_processors: [{ type: "regex_filter", config: { patterns: ["x"] } }, { type: "length_cap", config: {} }],
  context_fields: [{ name: "plan", type: "string", required: true, description: "", max_length: 20, values: [] }],
  memory: { history: { last_turns: 8 }, working: { template: "t" }, observational: null, semantic_recall: { top_k: 3 } },
};
const specOf = (name: string, extra: object = {}): AgentSpec =>
  ({ spec_version: 1, name, instructions: `${name} instructions {{context.plan}}`, model: MODEL, tools: [], ...extra }) as AgentSpec;

const V1 = specOf("Support bot", { instructions: "Version one text", tools: ["create_ticket"] });
const V2 = specOf("Support bot", { ...RICH, instructions: "Version two text {{context.plan}}", model: { provider: "test", name: "echo" } });
const DRAFT = specOf("Support bot", { instructions: "Draft text only", model: { provider: "openai", name: "gpt-4.1" }, tools: ["lookup_order"] });
const versionOf = (agentId: string, version: number, spec: AgentSpec): AgentVersion => ({
  agent_id: agentId, version, spec, spec_hash: `hash${version}`, created_at: "2026-11-14T00:00:00Z", created_by: "dev",
});

let agents: Agent[];
let versions: Record<string, AgentVersion[]>;
let media: MatchMediaControl | undefined;

const trace = (id: string, extra: Partial<TraceRun> = {}): TraceRun =>
  ({
    run_id: id, agent_id: "a1", status: "completed", display_status: "completed", version: 2, draft: false,
    created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:05Z", conversation_id: null, turn: null,
    model_id: "openai:gpt-4.1-mini", model_overridden: false, input_tokens: 10, output_tokens: 5, cost_usd: "0.001",
    ...extra,
  }) as unknown as TraceRun;

// A desktop window: the Chat tab's threads and chat fit beside Config (jsdom's 1024 px does not).
let restoreWidth: () => void = () => {};
beforeEach(() => {
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  window.localStorage.clear();
  window.history.replaceState({}, "", routes.agentChat("a1"));
  nav.path = routes.agentChat("a1");
  nav.push.mockReset();
  nav.replace.mockReset();
  vi.clearAllMocks();
  agents = [
    { id: "a1", name: "Support bot", draft_spec: DRAFT, latest_version: 2 },
    { id: "a2", name: "Billing bot", draft_spec: specOf("Billing bot"), latest_version: null },
  ];
  versions = { a1: [versionOf("a1", 2, V2), versionOf("a1", 1, V1)], a2: [] };
  vi.mocked(api.agents.get).mockImplementation(async (id: string) => agents.find((a) => a.id === id)!);
  vi.mocked(api.agents.list).mockImplementation(async () => agents);
  vi.mocked(api.versions.list).mockImplementation(async (id: string) => versions[id] ?? []);
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null });
  vi.mocked(listTraceRuns).mockResolvedValue({ items: [trace("r1")], next_before: null });
  vi.mocked(getRequestContext).mockImplementation(async (agentId, version) => ({
    agent_id: agentId, version: typeof version === "number" ? version : null, draft: version === "draft",
    fields: [], schema: { type: "object" as const, properties: {} },
  }));
});

afterEach(() => {
  cleanup();
  media?.restore();
  media = undefined;
  restoreWidth();
  document.documentElement.classList.remove("dark");
});

type Tab = "chat" | "traces" | "editor";
function tabBody(agentId: string, tab: Tab, conversationId: string | null = null) {
  if (tab === "chat") return <ChatTab agentId={agentId} conversationId={conversationId} />;
  return tab === "traces" ? <TracesTab agentId={agentId} /> : <EditorTab agentId={agentId} />;
}
function page(agentId = "a1", tab: Tab = "chat", conversationId: string | null = null) {
  return (
    <FrameHarness>
      <AgentLayout agentId={agentId}>{tabBody(agentId, tab, conversationId)}</AgentLayout>
    </FrameHarness>
  );
}
function openPage(agentId = "a1", tab: Tab = "chat", wide = true, conversationId: string | null = null) {
  media?.restore();
  media = mockMatchMedia(wide);
  nav.path = tab === "chat" ? routes.agentChat(agentId, { conversation: conversationId }) : tab === "traces" ? routes.agentTraces(agentId) : routes.agentEditor(agentId);
  return render(page(agentId, tab, conversationId));
}
const main = () => screen.getByRole("main");
const config = () => screen.findByRole("complementary", { name: "Config" });
// Tabs, Config sections, memory badges and run rows are found by role and name (their test ids are
// built dynamically, which the test-id inventory does not allow a test to query).
// By name: the Chat tab's href carries the chat's conversation and options (SL.6 review fix).
const tab = (name: string) => within(within(main()).getByRole("navigation", { name: "Agent sections" })).getByRole("link", { name: new RegExp(`^${name}$`, "i") });
const section = (name: string | RegExp) => screen.getByRole("region", { name });
const badge = (label: string) => within(screen.getByTestId("memory-card")).getByText(label).closest("[data-state]")!;
// SL.12: a row opens the trace Sheet in place, so its primary control is a button (the Trace column keeps a link).
const traceRows = () => within(screen.getByTestId("agent-traces-list")).getAllByRole("button", { name: /^Run started/ });

/** What a server page redirects to (Next's redirect() throws). */
async function redirectOf(run: () => Promise<unknown>): Promise<string | null> {
  try {
    await run();
    return null;
  } catch (error) {
    return (error as { redirectTo?: string }).redirectTo ?? null;
  }
}

describe("redirects, end to end", () => {
  const serverPlayground = async (query: Record<string, string>) => {
    const { default: Page } = await import("@/app/playground/page");
    return redirectOf(() => Page({ searchParams: Promise.resolve(query) }));
  };

  it.each([
    [{ agent: "a1" }, routes.agentChat("a1")],
    [{ agent: "a1", conversation: "c1" }, routes.agentChat("a1", { conversation: "c1" })],
    [{ agent: "a1", version: "3" }, routes.agentChat("a1", { version: 3 })],
    [{ agent: "a1", version: "draft", model: "test:echo" }, routes.agentChat("a1", { version: "draft", model: "test:echo" })],
    [{ agent: "a1", version: "junk" }, routes.agentChat("a1")],
    [{ agent: "a1", context: "preset" }, routes.agentChat("a1", { context: "preset" })],
    [
      { agent: "a2", conversation: "c9", version: "2", model: "openai:gpt-4.1", context: "preset" },
      routes.agentChat("a2", { conversation: "c9", version: 2, model: "openai:gpt-4.1", context: "preset" }),
    ],
  ])("/playground with %j goes to the chat route with the same options", async (query, target) => {
    expect(await serverPlayground(query)).toBe(target);
    // The target is the chat route: conversation in the path, options in the query, no legacy keys.
    const url = new URL(target, "http://x");
    expect(url.pathname).toMatch(/^\/agents\/a\d\/chat(\/c\d)?$/);
    expect(url.searchParams.has("agent")).toBe(false);
    expect(url.searchParams.has("conversation")).toBe(false);
  });

  it("/agents/:id opens the chat, /agents/new is not redirected, and the chat route is not a legacy URL", async () => {
    const { default: Index } = await import("@/app/agents/[id]/page");
    const go = (id: string) => redirectOf(() => Index({ params: Promise.resolve({ id }), searchParams: Promise.resolve({}) }));
    expect(await go("a1")).toBe(routes.agentChat("a1"));
    expect(await go("agent with space/ü")).toBe(routes.agentChat("agent with space/ü"));
    expect(routes.agentChat("agent with space/ü")).not.toContain(" ");
  });

  it("a bare /playground picks the last-used agent, then the first, and the chat then opens that agent", async () => {
    window.localStorage.setItem(LAST_AGENT_KEY, "a2");
    expect(await playgroundClientTarget("")).toBe(routes.agentChat("a2"));
    // A last agent that was deleted falls back to the first one.
    window.localStorage.setItem(LAST_AGENT_KEY, "deleted");
    expect(await playgroundClientTarget("")).toBe(routes.agentChat("a1"));
    window.localStorage.clear();
    expect(await playgroundClientTarget("")).toBe(routes.agentChat("a1"));
    // Options are carried to whichever agent was picked.
    window.localStorage.setItem(LAST_AGENT_KEY, "a2");
    expect(await playgroundClientTarget("?version=draft&model=test%3Aecho&context=preset")).toBe(
      routes.agentChat("a2", { version: "draft", model: "test:echo", context: "preset" }),
    );
    agents = [];
    expect(await playgroundClientTarget("")).toBe(routes.agents());
  });

  it("the bare /playground page redirects the router with replace, and following it lands on the agent's chat", async () => {
    window.localStorage.setItem(LAST_AGENT_KEY, "a2");
    window.history.replaceState({}, "", `${routes.playground()}?model=test%3Aecho`);
    const { default: Page } = await import("@/app/playground/page");
    render((await Page({ searchParams: Promise.resolve({ model: "test:echo" }) })) as React.ReactElement);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledTimes(1));
    const target = nav.replace.mock.calls[0][0] as string;
    expect(target).toBe(routes.agentChat("a2", { model: "test:echo" }));
    cleanup();
    window.history.replaceState({}, "", target);
    openPage("a2");
    await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Billing bot"));
    expect(window.localStorage.getItem(LAST_AGENT_KEY)).toBe("a2");
  });

  it("an old conversation link without an agent lands in that conversation's agent chat and loads it", async () => {
    const conv = { id: "c1", agent_id: "a2", title: "Old", turn_count: 0, created_at: "", updated_at: "", runs: [] };
    vi.mocked(getConversation).mockResolvedValue(conv);
    const target = await playgroundClientTarget("?conversation=c1&version=draft");
    expect(target).toBe(routes.agentChat("a2", { conversation: "c1", version: "draft" }));
    cleanup();
    window.history.replaceState({}, "", target);
    openPage("a2", "chat", true, "c1");
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith("c1"));
    expect(screen.queryByTestId("chat-other-agent")).toBeNull();
    expect(window.location.pathname).toBe(routes.agentChat("a2", { conversation: "c1" }));
  });

  it("the legacy ?conversation= on a chat URL still loads that conversation, and the route's conversation wins", async () => {
    const conv = (id: string) => ({ id, agent_id: "a1", title: id, turn_count: 0, created_at: "", updated_at: "", runs: [] });
    vi.mocked(getConversation).mockImplementation(async (id: string) => conv(id));
    window.history.replaceState({}, "", `${routes.agentChat("a1")}?conversation=old-c`);
    openPage("a1");
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith("old-c"));
    cleanup();
    vi.mocked(getConversation).mockClear();
    window.history.replaceState({}, "", `${routes.agentChat("a1", { conversation: "path-c" })}?conversation=old-c`);
    openPage("a1", "chat", true, "path-c");
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith("path-c"));
    expect(getConversation).not.toHaveBeenCalledWith("old-c");
  });
});

describe("tabs and the switcher", () => {
  const switchTo = async (name: string) => {
    fireEvent.click(await screen.findByRole("combobox", { name: "Switch agent" }));
    fireEvent.click(await screen.findByRole("option", { name }));
  };

  it("the tab links keep the agent, mark the current tab, and the agent loads once across tabs", async () => {
    const view = openPage("a1", "chat");
    await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot"));
    expect(api.agents.get).toHaveBeenCalledTimes(1);
    const tabs = () => within(main()).getByRole("navigation", { name: "Agent sections" });
    expect(tab("chat")).toHaveAttribute("aria-current", "page");
    nav.path = routes.agentTraces("a1");
    view.rerender(page("a1", "traces"));
    expect(await screen.findByTestId("agent-traces-tab")).toBeInTheDocument();
    expect(tab("traces")).toHaveAttribute("aria-current", "page");
    expect(tab("chat")).not.toHaveAttribute("aria-current");
    expect(within(tabs()).getByRole("link", { name: "Editor" })).toHaveAttribute("href", routes.agentEditor("a1"));
    // The layout (and its loaded agent) persists across the tab change.
    expect(api.agents.get).toHaveBeenCalledTimes(1);
    expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot");
  });

  it.each([
    ["chat", routes.agentChat("a2")],
    ["traces", routes.agentTraces("a2")],
    ["editor", routes.agentEditor("a2")],
  ] as const)("the switcher on the %s tab goes to the same tab of the other agent", async (tab, href) => {
    openPage("a1", tab);
    await switchTo("Billing bot");
    expect(nav.push).toHaveBeenCalledWith(href);
  });

  it("switching agent from a conversation drops that conversation (it belongs to the first agent) and the options", async () => {
    window.history.replaceState({}, "", routes.agentChat("a1", { conversation: "c1", version: 1, model: "test:echo" }));
    vi.mocked(getConversation).mockResolvedValue({ id: "c1", agent_id: "a1", title: "T", turn_count: 0, created_at: "", updated_at: "", runs: [] });
    openPage("a1", "chat", true, "c1");
    await switchTo("Billing bot");
    expect(nav.push).toHaveBeenCalledWith(routes.agentChat("a2"));
  });

  it("the switcher lists every agent and marks the current one; opening another agent's page re-titles it", async () => {
    const view = openPage("a1", "traces");
    fireEvent.click(await screen.findByRole("combobox", { name: "Switch agent" }));
    const options = await screen.findAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["Support bot", "Billing bot"]);
    fireEvent.keyDown(document.body, { key: "Escape" });
    nav.path = routes.agentTraces("a2");
    view.rerender(page("a2", "traces"));
    await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Billing bot"));
    await waitFor(() => expect(document.title).toContain("Billing bot"));
    expect(listTraceRuns).toHaveBeenLastCalledWith(expect.objectContaining({ agent_id: "a2" }), expect.anything());
  });

  it("the Config panel follows the chat's version on the Chat tab only; other tabs show the latest", async () => {
    window.history.replaceState({}, "", routes.agentChat("a1", { version: "draft" }));
    const view = openPage("a1", "chat");
    await waitFor(() => expect(within(screen.getByRole("complementary", { name: "Config" })).getByTestId("config-version")).toHaveTextContent("Draft"));
    nav.path = routes.agentTraces("a1");
    view.rerender(page("a1", "traces"));
    await waitFor(() => expect(screen.getByTestId("config-version")).toHaveTextContent("v2 (latest)"));
  });
});

describe("panes", () => {
  const key = (init: KeyboardEventInit, target: Element | Document = document.body) => fireEvent.keyDown(target, init);
  const threadsToggle = () => screen.getByTestId("agent-threads-toggle");
  const configToggle = () => screen.getByTestId("agent-config-toggle");

  it("`{` and `]` do nothing while typing in any field, with a modifier, or `{` off the Chat tab", async () => {
    openPage("a1", "chat");
    await config();
    const composer = await screen.findByRole("textbox", { name: "Message" });
    for (const k of ["{", "]"]) key({ key: k }, composer);
    key({ key: "]", ctrlKey: true });
    key({ key: "{", metaKey: true });
    key({ key: "]", altKey: true });
    expect(threadsToggle()).toHaveAttribute("aria-pressed", "true");
    expect(configToggle()).toHaveAttribute("aria-pressed", "true");
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    key({ key: "]" }, input);
    expect(configToggle()).toHaveAttribute("aria-pressed", "true");
    input.remove();
    key({ key: "]" });
    await waitFor(() => expect(configToggle()).toHaveAttribute("aria-pressed", "false"));
    cleanup();
    openPage("a1", "traces");
    await screen.findByTestId("agent-traces-tab");
    expect(screen.queryByTestId("agent-threads-toggle")).toBeNull();
    const before = configToggle().getAttribute("aria-pressed");
    key({ key: "{" });
    expect(configToggle().getAttribute("aria-pressed")).toBe(before);
    key({ key: "]" });
    await waitFor(() => expect(configToggle().getAttribute("aria-pressed")).not.toBe(before));
  });

  it("the header buttons toggle the panes and carry the keys as shortcuts", async () => {
    openPage("a1", "chat");
    await config();
    fireEvent.click(configToggle());
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull());
    fireEvent.click(threadsToggle());
    expect(screen.queryByRole("navigation", { name: "Conversations" })).toBeNull();
    fireEvent.click(threadsToggle());
    expect(await screen.findByRole("navigation", { name: "Conversations" })).toBeInTheDocument();
  });

  it("saves the collapse per agent: collapsing a1's threads leaves a2 expanded, and a1 stays collapsed on reopening", async () => {
    openPage("a1", "chat");
    await config();
    key({ key: "{" });
    await waitFor(() => expect(JSON.parse(window.localStorage.getItem(agentLayoutKey("a1")) ?? "{}").threads).toBe(0));
    expect(window.localStorage.getItem(agentLayoutKey("a2"))).toBeNull();
    cleanup();
    openPage("a2", "chat");
    await screen.findByRole("separator", { name: "Resize conversations panel" });
    expect(screen.getByTestId("agent-threads-toggle")).toHaveAttribute("aria-pressed", "true");
    cleanup();
    openPage("a1", "chat");
    await waitFor(() => expect(screen.getByTestId("agent-threads-toggle")).toHaveAttribute("aria-pressed", "false"));
  });

  it("a broken saved layout is ignored and the default one shows", async () => {
    window.localStorage.setItem(agentLayoutKey("a1"), "{not json");
    openPage("a1", "chat");
    await screen.findByRole("separator", { name: "Resize conversations panel" });
    expect(screen.getByTestId("agent-threads-toggle")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("agent-chat-panes")).toHaveAttribute("data-layout", "wide");
  });
});

describe("below 1024 px", () => {
  const key = (init: KeyboardEventInit) => fireEvent.keyDown(document.body, init);

  it("uses Sheets: threads and Config are both closed at first, and Config opens from the header button", async () => {
    openPage("a1", "chat", false);
    await waitFor(() => expect(screen.getByTestId("agent-chat-panes")).toHaveAttribute("data-layout", "narrow"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("agent-threads-pane")).toBeNull();
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();
    expect(screen.getByTestId("agent-config-toggle")).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByTestId("agent-config-toggle"));
    const sheet = await screen.findByRole("dialog", { name: "Config" });
    expect(within(sheet).getByTestId("config-panel")).toBeInTheDocument();
    expect(screen.getByTestId("agent-config-toggle")).toHaveAttribute("aria-pressed", "true");
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByTestId("agent-config-toggle")).toHaveAttribute("aria-pressed", "false");
  });

  it("`]` and `{` open the Config and threads Sheets; picking a conversation closes the threads Sheet", async () => {
    vi.mocked(listConversations).mockResolvedValue({
      items: [{ id: "c1", agent_id: "a1", title: "Earlier chat", turn_count: 1, created_at: "2026-11-14T00:00:00Z", updated_at: "2026-11-14T00:00:00Z" }],
      next_before: null,
    });
    vi.mocked(getConversation).mockResolvedValue({ id: "c1", agent_id: "a1", title: "Earlier chat", turn_count: 0, created_at: "", updated_at: "", runs: [] });
    openPage("a1", "chat", false);
    await screen.findByTestId("agent-threads-toggle");
    key({ key: "{" });
    const threads = await screen.findByRole("dialog", { name: "Conversations" });
    expect(within(threads).getByTestId("memory-card")).toBeInTheDocument();
    fireEvent.click(await within(threads).findByRole("button", { name: /^Earlier chat/ }));
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith("c1"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    key({ key: "]" });
    expect(await screen.findByRole("dialog", { name: "Config" })).toBeInTheDocument();
  });

  it("crossing 1024 px swaps the Sheets for the panes and back, closing any open Sheet", async () => {
    openPage("a1", "chat", false);
    await screen.findByTestId("agent-threads-toggle");
    fireEvent.click(screen.getByTestId("agent-threads-toggle"));
    await screen.findByRole("dialog", { name: "Conversations" });
    act(() => media!.setWide(true));
    await waitFor(() => expect(screen.getByTestId("agent-chat-panes")).toHaveAttribute("data-layout", "wide"));
    expect(screen.queryByRole("dialog")).toBeNull();
    // Config stays closed (the SL.4 rule: it never opens by itself when the screen grows); the button opens it as a column.
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();
    fireEvent.click(screen.getByTestId("agent-config-toggle"));
    expect(await config()).toBeInTheDocument();
    act(() => media!.setWide(false));
    await waitFor(() => expect(screen.getByTestId("agent-chat-panes")).toHaveAttribute("data-layout", "narrow"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();
  });

  it("at phone width nothing is pinned wider than the screen and the page can shrink", async () => {
    const original = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 375 });
    try {
      openPage("a1", "chat", false);
      await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot"));
      const root = screen.getByTestId("agent-page");
      expect(root).toHaveClass("min-w-0");
      expect(screen.getByTestId("agent-chat-panes")).toHaveClass("min-w-0");
      expect(screen.queryByTestId("agent-threads-pane")).toBeNull();
      // jsdom has no layout, so look for what could force an overflow: fixed widths past the screen.
      const fixed = Array.from(root.querySelectorAll<HTMLElement>("*")).flatMap((el) =>
        Array.from(el.classList).filter((c) => /^(min-)?w-\[(\d+)px\]$/.test(c) && Number(/\[(\d+)px\]/.exec(c)![1]) > 375),
      );
      expect(fixed).toEqual([]);
      expect(Array.from(root.querySelectorAll<HTMLElement>("[style]")).filter((el) => Number.parseFloat(el.style.minWidth) > 375)).toEqual([]);
      // The tab strip stays reachable: it scrolls on its own instead of widening the page.
      const tabs = within(main()).getByRole("navigation", { name: "Agent sections" });
      expect(tabs.querySelectorAll("a")).toHaveLength(3);
      expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(document.documentElement.clientWidth || 375);
    } finally {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: original });
    }
  });
});

describe("the Config panel is read-only and matches the version", () => {
  const state = (label: string) => badge(label).getAttribute("data-state");

  it("latest published (v2): model, instructions with the placeholder marked, tools, context fields, processors, memory badges", async () => {
    openPage("a1", "chat");
    const panel = within(await config());
    await waitFor(() => expect(panel.getByTestId("config-version")).toHaveTextContent("v2 (latest)"));
    expect(panel.getByTestId("config-model")).toHaveTextContent("test:echo");
    expect(panel.getByTestId("config-instructions")).toHaveTextContent("Version two text {{context.plan}}");
    expect(panel.getByTestId("config-instructions").querySelector("mark")).toHaveTextContent("{{context.plan}}");
    const tools = within(section(/^Tools/));
    expect(tools.getByText("create_ticket")).toBeInTheDocument();
    expect(tools.getByText("lookup_order")).toBeInTheDocument();
    expect(tools.getByText(/find_customer/)).toHaveTextContent("MCP crm");
    expect(within(section(/^Request context fields/)).getByText("plan")).toBeInTheDocument();
    const processors = section(/^Processors/);
    expect(within(processors).getByText("pii_redactor")).toBeInTheDocument();
    expect(within(processors).getByText("regex_filter, length_cap")).toBeInTheDocument();
    expect(state("History 8 turns")).toBe("on");
    expect([state("Working memory"), state("Observational"), state("Semantic recall")]).toEqual(["on", "off", "on"]);
    expect(screen.getByTestId("memory-card")).not.toContainElement(panel.getByTestId("config-panel"));
  });

  it("an older version (?version=1) and the draft (?version=draft) each show their own spec, not the latest's", async () => {
    window.history.replaceState({}, "", routes.agentChat("a1", { version: 1 }));
    openPage("a1", "chat");
    await waitFor(() => expect(screen.getByTestId("config-version")).toHaveTextContent("v1"));
    expect(screen.getByTestId("config-version")).not.toHaveTextContent("latest");
    expect(screen.getByTestId("config-model")).toHaveTextContent("openai:gpt-4.1-mini");
    expect(screen.getByTestId("config-instructions")).toHaveTextContent("Version one text");
    expect(within(section(/^Tools/)).queryByText("lookup_order")).toBeNull();
    expect([state("Working memory"), state("Observational"), state("Semantic recall")]).toEqual(["off", "off", "off"]);
    cleanup();
    window.history.replaceState({}, "", routes.agentChat("a1", { version: "draft" }));
    openPage("a1", "chat");
    await waitFor(() => expect(screen.getByTestId("config-version")).toHaveTextContent("Draft"));
    expect(screen.getByTestId("config-model")).toHaveTextContent("openai:gpt-4.1");
    expect(screen.getByTestId("config-instructions")).toHaveTextContent("Draft text only");
    expect(within(section(/^Tools/)).getByText("lookup_order")).toBeInTheDocument();
  });

  it("an agent that was never published shows its draft; the panel has no inputs, only links to the Editor", async () => {
    openPage("a2", "chat");
    const panel = within(await config());
    await waitFor(() => expect(panel.getByTestId("config-version")).toHaveTextContent("Draft"));
    expect(panel.getByTestId("config-instructions")).toHaveTextContent("Billing bot instructions");
    const root = panel.getByTestId("config-panel");
    expect(root.querySelectorAll("input, textarea, select")).toHaveLength(0);
    const links = Array.from(root.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(links.length).toBeGreaterThan(0);
    links.forEach((href) => expect(href).toMatch(/^\/agents\/a2\/editor(\?|$)/));
  });
});

describe("Copy link", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("writes the current address to the clipboard and announces it", async () => {
    const writeText = vi.fn(async (_text: string) => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    window.history.replaceState({}, "", routes.agentChat("a1", { conversation: "c1", version: 1 }));
    openPage("a1", "chat", true, "c1");
    fireEvent.click(await screen.findByTestId("agent-copy-link"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(window.location.href));
    expect((writeText.mock.calls[0] as unknown[])[0]).toContain("/agents/a1/chat/c1?version=1");
    await waitFor(() => expect(screen.getAllByRole("status").map((n) => n.textContent)).toContain("Link copied"));
  });

  it("says copying is unavailable when the clipboard refuses", async () => {
    const writeText = vi.fn(async () => { throw new Error("denied"); });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    openPage("a1", "traces");
    fireEvent.click(await screen.findByTestId("agent-copy-link"));
    await waitFor(() => expect(screen.getAllByRole("status").map((n) => n.textContent)).toContain("Copying is not available here"));
  });
});

describe("the Traces tab", () => {
  it("lists only this agent's runs, each opening its trace, with a link to the full Traces filtered to the agent", async () => {
    vi.mocked(listTraceRuns).mockResolvedValue({
      items: [trace("r1"), trace("r2", { conversation_id: "c7", turn: 2, display_status: "failed", status: "failed" })],
      next_before: null,
    });
    openPage("a1", "traces");
    await screen.findByTestId("agent-traces-list");
    await waitFor(() => expect(traceRows()).toHaveLength(2));
    expect(listTraceRuns).toHaveBeenCalledWith({ ...EMPTY_FILTERS, agent_id: "a1" }, expect.objectContaining({ limit: 50 }));
    const hrefs = within(screen.getByTestId("agent-traces-list")).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(hrefs).toContain(routes.trace("r1"));
    expect(hrefs).toContain(routes.trace("r2"));
    expect(screen.getByTestId("agent-trace-conversation")).toHaveAttribute("href", routes.agentChat("a1", { conversation: "c7" }));
    const all = screen.getByTestId("agent-traces-all");
    expect(all).toHaveAttribute("href", routes.traces(traceFiltersToParams({ ...EMPTY_FILTERS, agent_id: "a1" })));
    expect(all.getAttribute("href")).toContain("agent_id=a1");
  });

  it("says so when the agent has no runs, offers Retry after a failure, and loads more with `before`", async () => {
    vi.mocked(listTraceRuns).mockResolvedValueOnce({ items: [], next_before: null });
    const view = openPage("a1", "traces");
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
    view.unmount();
    vi.mocked(listTraceRuns).mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce({ items: [trace("r9")], next_before: "cursor-1" });
    openPage("a1", "traces");
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(traceRows()).toHaveLength(1));
    vi.mocked(listTraceRuns).mockResolvedValueOnce({ items: [trace("r10")], next_before: null });
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(traceRows()).toHaveLength(2));
    expect(listTraceRuns).toHaveBeenLastCalledWith(expect.objectContaining({ agent_id: "a1" }), expect.objectContaining({ before: "cursor-1" }));
  });
});

describe("the Editor tab saves and the layout reloads", () => {
  it("Save draft updates the page title, the document title and the Config panel without a navigation", async () => {
    // The editor shows the draft; after a save the layout re-reads the agent from the server.
    vi.mocked(api.agents.update).mockImplementation(async (id: string, spec: AgentSpec) => {
      const saved = { ...agents[0], name: spec.name, draft_spec: spec };
      agents = [saved, agents[1]];
      return saved;
    });
    openPage("a1", "editor");
    await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot"));
    const input = await screen.findByDisplayValue("Support bot");
    const loads = vi.mocked(api.agents.get).mock.calls.length;
    fireEvent.change(input, { target: { value: "Support bot 2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalledWith("a1", expect.objectContaining({ name: "Support bot 2" })));
    await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot 2"));
    await waitFor(() => expect(document.title).toContain("Support bot 2"));
    expect(vi.mocked(api.agents.get).mock.calls.length).toBeGreaterThan(loads);
    expect(nav.push).not.toHaveBeenCalled();
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("Publish reloads the versions, so the Config panel's latest label moves to the new version", async () => {
    vi.mocked(api.versions.create).mockImplementation(async () => {
      const created = versionOf("a1", 3, DRAFT);
      versions = { ...versions, a1: [created, ...versions.a1] };
      agents = [{ ...agents[0], latest_version: 3 }, agents[1]];
      return created;
    });
    openPage("a1", "editor");
    fireEvent.click(await screen.findByTestId("agent-config-toggle")); // Config is closed by default off the Chat tab
    await waitFor(() => expect(screen.getByTestId("config-version")).toHaveTextContent("v2 (latest)"));
    fireEvent.click(await screen.findByRole("button", { name: "Publish" }));
    expect(await screen.findByText("Version 3 published")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("config-version")).toHaveTextContent("v3 (latest)"));
  });
});

describe("the agents list opens the chat", () => {
  it("each row links to its agent's chat; following it opens that agent's page with its chat tab current", async () => {
    const { default: AgentsPage } = await import("@/app/agents/page");
    media = mockMatchMedia(true);
    nav.path = routes.agents();
    render(
      <FrameHarness>
        <AgentsPage />
      </FrameHarness>,
    );
    const row = await screen.findByRole("link", { name: "Billing bot" });
    expect(row).toHaveAttribute("href", routes.agentChat("a2"));
    expect(screen.getByRole("link", { name: "Support bot" })).toHaveAttribute("href", routes.agentChat("a1"));
        const target = row.getAttribute("href")!;
    cleanup();
    window.history.replaceState({}, "", target);
    openPage("a2", "chat");
    await waitFor(() => expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Billing bot"));
    expect(tab("chat")).toHaveAttribute("aria-current", "page");
    expect(window.localStorage.getItem(LAST_AGENT_KEY)).toBe("a2");
  });
});

describe.each(["light", "dark"] as const)("accessibility (%s theme)", (theme) => {
  beforeEach(() => {
    window.localStorage.setItem("studio.theme", theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.lang = "en"; // app/layout.tsx sets it; jsdom's bare <html> has none
  });
  afterEach(() => {
    document.documentElement.removeAttribute("lang");
    document.documentElement.style.colorScheme = "";
  });

  it.each(["chat", "traces", "editor"] as const)("the %s tab has exactly one h1 (the agent), one main, and no axe violations", async (tab) => {
    openPage("a1", tab);
    await waitFor(() => expect(within(main()).getAllByRole("heading", { level: 1 })).toHaveLength(1));
    expect(within(main()).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot");
    expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark");
    const settled = { chat: "agent-chat", traces: "agent-traces-list", editor: "agent-editor-tab" }[tab];
    await screen.findByTestId(settled);
    if (tab === "chat") await waitFor(() => expect(screen.getByTestId("config-version")).toHaveTextContent("v2"));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(document.querySelectorAll("main, [role='main']")).toHaveLength(1);
    expect(within(main()).getAllByRole("heading", { level: 1 })).toHaveLength(1);
    await expectNoAxeViolations(document, { rules: { "landmark-one-main": { enabled: true }, region: { enabled: true } } });
  }, AXE_TIMEOUT_MS);

  it("with the Config and threads Sheets open below 1024 px there are no axe violations either", async () => {
    openPage("a1", "chat", false);
    fireEvent.click(await screen.findByTestId("agent-config-toggle"));
    await screen.findByRole("dialog", { name: "Config" });
    await waitFor(() => expect(screen.getByTestId("config-version")).toHaveTextContent("v2"));
    await expectNoAxeViolations(document.body);
    fireEvent.keyDown(screen.getByRole("dialog", { name: "Config" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByTestId("agent-threads-toggle"));
    await screen.findByRole("dialog", { name: "Conversations" });
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
