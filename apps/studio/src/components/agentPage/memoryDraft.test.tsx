/**
 * RM.18 review (blocking 1): an unsaved working memory edit in the threads pane's inspector is never
 * lost silently. The edit lives in the chat (above the panes), and every way of hiding it asks the
 * Discard dialog first: the narrow threads Sheet's Escape, the card's Hide, `{` and the header
 * toggle. Crossing 1024 px remounts the panes and keeps the edit.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/agents/a1/chat/c7",
  useParams: () => ({ id: "a1" }),
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
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
  deleteConversation: vi.fn(),
}));
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
  return {
    ...original,
    getRequestContext: vi.fn(async () => ({ agent_id: "a1", version: 1, draft: false, fields: [], schema: { type: "object", properties: {} } })),
    validateRequestContext: vi.fn(async (_a: string, v: unknown) => v),
  };
});

import { api, type Agent, type AgentSpec } from "@/lib/api";
import { getConversation, listConversations } from "@/lib/conversations";
import { routes } from "@/lib/routes";
import { FrameHarness, mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH, type MatchMediaControl } from "@/components/frame/frameTesting";
import { MEMORY_CARD_KEY } from "@/components/memory/memoryModel";
import { AgentLayout } from "./AgentLayout";
import { ChatTab } from "./ChatTab";

const SPEC = {
  spec_version: 1, name: "Support bot", instructions: "Help.", model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [],
  memory: { working: { scope: "conversation" } },
} as unknown as AgentSpec;
const AGENT: Agent = { id: "a1", name: "Support bot", draft_spec: SPEC, latest_version: 1 };
const T0 = "2026-11-14T10:00:00Z";
const conversation = (id: string) => ({
  id, agent_id: "a1", resource_id: "bob", title: `Chat ${id}`, turn_count: 1, last_turn_run_id: null, last_turn_status: "completed",
  last_turn_display_status: "completed", created_at: T0, updated_at: T0,
});

let media: MatchMediaControl | undefined;
let restoreWidth: () => void = () => {};

beforeEach(() => {
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.localStorage.setItem(MEMORY_CARD_KEY, "open");
  window.history.replaceState({}, "", routes.agentChat("a1", { conversation: "c7" }));
  vi.mocked(api.agents.get).mockResolvedValue(AGENT);
  vi.mocked(api.agents.list).mockResolvedValue([AGENT]);
  vi.mocked(api.versions.list).mockResolvedValue([{ agent_id: "a1", version: 1, spec: SPEC, spec_hash: "h", created_at: T0, created_by: "dev" }]);
  vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c7"), conversation("c8")], next_before: null } as never);
  vi.mocked(getConversation).mockImplementation(async (id) => ({ ...conversation(id), runs: [] }) as never);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      const path = new URL(input).pathname;
      const body = path.endsWith("/memory/working")
        ? { agent_id: "a1", scope: "conversation", scope_key: "c7", format: "markdown", content: "c7 note", version: 2, updated_by_run_id: null, created_at: T0, updated_at: T0 }
        : { detail: "Not found" };
      return new Response(JSON.stringify(body), { status: path.endsWith("/memory/working") ? 200 : 404, headers: { "content-type": "application/json" } });
    }),
  );
});

afterEach(() => {
  cleanup();
  media?.restore();
  media = undefined;
  restoreWidth();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function renderChat(wide: boolean) {
  media = mockMatchMedia(wide);
  return render(
    <FrameHarness>
      <AgentLayout agentId="a1">
        <ChatTab agentId="a1" conversationId="c7" />
      </AgentLayout>
    </FrameHarness>,
  );
}

const textarea = () => screen.queryByTestId("memory-working-textarea") as HTMLTextAreaElement | null;
const discardDialog = () => screen.findByRole("dialog", { name: "Discard the working memory edit?" });

async function dirtyEdit() {
  fireEvent.click(await screen.findByTestId("memory-working-edit"));
  fireEvent.change(textarea()!, { target: { value: "unsaved text" } });
}

async function stay() {
  fireEvent.click(within(await discardDialog()).getByRole("button", { name: "Stay" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard the working memory edit?" })).toBeNull());
}

async function discard() {
  fireEvent.click(within(await discardDialog()).getByRole("button", { name: "Discard edit" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard the working memory edit?" })).toBeNull());
}

describe("an edit of a note no longer on screen is not asked about", () => {
  it("the version chip moves to a spec with the other scope: no question; moving back shows the edit and asks again", async () => {
    // Latest (v1): per conversation. The draft: per user (a different note).
    const draft = { ...SPEC, memory: { working: { scope: "resource" } } } as unknown as AgentSpec;
    vi.mocked(api.agents.get).mockResolvedValue({ ...AGENT, draft_spec: draft });
    renderChat(true);
    await dirtyEdit();
    const version = () => screen.getByRole("combobox", { name: "Version" });
    fireEvent.change(version(), { target: { value: "draft" } });
    await waitFor(() => expect(textarea()).toBeNull());
    fireEvent.click(screen.getByTestId("memory-card-toggle"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("memory-card-toggle")).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(screen.getByTestId("memory-card-toggle"));
    fireEvent.change(version(), { target: { value: "latest" } });
    await waitFor(() => expect(textarea()).toHaveValue("unsaved text"));
    fireEvent.click(screen.getByTestId("memory-card-toggle"));
    await discardDialog();
  });
});

describe("an unsaved working memory edit survives or asks", () => {
  it("narrow: Escape on the threads Sheet asks; Stay keeps the Sheet and the edit, Discard edit closes it", async () => {
    renderChat(false);
    fireEvent.click(await screen.findByTestId("agent-threads-toggle"));
    await screen.findByTestId("agent-threads-sheet");
    await dirtyEdit();
    fireEvent.keyDown(textarea()!, { key: "Escape" });
    expect(await discardDialog()).toHaveTextContent("Closing the conversations pane discards it.");
    await stay();
    expect(screen.getByTestId("agent-threads-sheet")).toBeInTheDocument();
    expect(textarea()).toHaveValue("unsaved text");
    fireEvent.keyDown(textarea()!, { key: "Escape" });
    await discard();
    await waitFor(() => expect(screen.queryByTestId("agent-threads-sheet")).toBeNull());
    // Reopened: the edit was discarded, the note shows.
    fireEvent.click(screen.getByTestId("agent-threads-toggle"));
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("c7 note");
    expect(textarea()).toBeNull();
  });

  it("the card's Hide asks; Stay keeps the edit, Discard edit hides the inspector", async () => {
    renderChat(true);
    await dirtyEdit();
    fireEvent.click(screen.getByTestId("memory-card-toggle"));
    expect(await discardDialog()).toHaveTextContent("Hiding the memory inspector discards it.");
    await stay();
    expect(textarea()).toHaveValue("unsaved text");
    expect(screen.getByTestId("memory-card-toggle")).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByTestId("memory-card-toggle"));
    await discard();
    expect(screen.getByTestId("memory-card-toggle")).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(screen.getByTestId("memory-card-toggle"));
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("c7 note");
  });

  it("`{` and the header's Conversations toggle ask before collapsing", async () => {
    renderChat(true);
    await dirtyEdit();
    act(() => textarea()!.blur());
    fireEvent.keyDown(window, { key: "{" });
    await stay();
    expect(screen.getByTestId("agent-threads-toggle")).toHaveAttribute("aria-pressed", "true");
    expect(textarea()).toHaveValue("unsaved text");
    fireEvent.click(screen.getByTestId("agent-threads-toggle"));
    await discard();
    await waitFor(() => expect(screen.getByTestId("agent-threads-toggle")).toHaveAttribute("aria-pressed", "false"));
    // Opening never asks.
    fireEvent.click(screen.getByTestId("agent-threads-toggle"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("crossing 1024 px remounts the panes and keeps the edit, both ways", async () => {
    renderChat(true);
    await dirtyEdit();
    act(() => media!.setWide(false));
    await waitFor(() => expect(screen.queryByTestId("agent-threads-pane")).toBeNull());
    fireEvent.click(await screen.findByTestId("agent-threads-toggle"));
    await screen.findByTestId("agent-threads-sheet");
    await waitFor(() => expect(textarea()).toHaveValue("unsaved text"));
    act(() => media!.setWide(true));
    await waitFor(() => expect(textarea()).toHaveValue("unsaved text"));
    // Still guarded after the remounts.
    fireEvent.click(screen.getByTestId("memory-card-toggle"));
    await discardDialog();
  });
});
