// W1.1: the chat's model settings popover sends its overrides through the turn's run options.
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatTab } from "@/components/agentPage/ChatTab";
import { api } from "@/lib/api";
import { createConversation, listConversations } from "@/lib/conversations";
import { listEvents, subscribeRun } from "@/lib/runs";
import { fetchModels, type ModelOption } from "@/lib/metrics";
import { getRequestContext, validateRequestContext } from "@/lib/requestContext";
import type { AgentSpec, AgentVersion } from "@/lib/api";
import { routes } from "@/lib/routes";
import { findSelectOption } from "@/test/select";

vi.mock("@/lib/api", () => ({
  BASE_URL: "http://api.test",
  apiHeaders: (extra: HeadersInit = {}) => extra,
  api: { agents: { list: vi.fn(), get: vi.fn() }, versions: { list: vi.fn() } },
}));
vi.mock("@/lib/metrics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/metrics")>()),
  fetchModels: vi.fn(),
}));
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
  listEvents: vi.fn(),
  subscribeRun: vi.fn(),
}));

const MODEL = { provider: "openai", name: "gpt-4.1-mini" };
const specOf = (name: string): AgentSpec => ({ spec_version: 1, name, instructions: "Help", model: MODEL, tools: [] }) as AgentSpec;
const agent = { id: "agent-1", name: "Support assistant", draft_spec: specOf("s"), latest_version: 1 };
const version1: AgentVersion = {
  agent_id: "agent-1", version: 1, spec: specOf("s"), spec_hash: "h1", created_at: "2026-11-12T00:00:00Z", created_by: "dev",
};
const modelList: ModelOption[] = [
  { id: "openai:gpt-4.1-mini", provider: "openai", name: "gpt-4.1-mini", wildcard: false, source: "allowlist", description: null },
  { id: "openai:gpt-4.1", provider: "openai", name: "gpt-4.1", wildcard: false, source: "allowlist", description: null },
];

let posts: Array<{ url: string; body: Record<string, unknown> }>;
let refusals: Array<{ status: number; detail: unknown }>;
const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
  const body = JSON.parse(String(init.body));
  posts.push({ url, body });
  const refusal = refusals.shift();
  if (refusal) return { ok: false, status: refusal.status, json: async () => ({ detail: refusal.detail }) };
  return {
    ok: true,
    status: 200,
    json: async () => ({
      run_id: body.run_id, status: "queued", display_status: "queued", agent_id: "agent-1", version: 1, draft: false,
      model_id: "openai:gpt-4.1-mini", model_overridden: false, conversation_id: "conv-new", turn: posts.length,
    }),
  };
});

const composer = () => screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;

async function renderReady() {
  render(<ChatTab agentId="agent-1" conversationId={null} />);
  await screen.findByRole("combobox", { name: "Version" });
  await findSelectOption("openai:gpt-4.1");
  await waitFor(() => expect(api.agents.get).toHaveBeenCalledWith("agent-1"));
}

async function send(text = "Hi") {
  const before = posts.length;
  fireEvent.change(composer(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(posts.length).toBe(before + 1));
}

const settingsButton = () => screen.getByRole("button", { name: /^Model settings:/ });
function setSetting(label: RegExp, value: string) {
  if (!screen.queryByTestId("model-settings-popover")) fireEvent.click(settingsButton());
  fireEvent.change(within(screen.getByTestId("model-settings-popover")).getByLabelText(label), { target: { value } });
}

beforeEach(() => {
  window.history.replaceState({}, "", routes.agentChat("agent-1"));
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  posts = [];
  refusals = [];
  vi.stubGlobal("fetch", fetchMock);
  vi.mocked(api.agents.list).mockResolvedValue([agent] as never);
  vi.mocked(api.agents.get).mockResolvedValue(agent as never);
  vi.mocked(api.versions.list).mockResolvedValue([version1]);
  vi.mocked(fetchModels).mockResolvedValue(modelList);
  vi.mocked(getRequestContext).mockImplementation(async (agentId, version) => ({
    agent_id: agentId, version: typeof version === "number" ? version : null, draft: version === "draft",
    fields: [], schema: { type: "object" as const, properties: {} },
  }));
  vi.mocked(validateRequestContext).mockImplementation(async (_a, value) => value);
  vi.mocked(createConversation).mockImplementation(async (agentId, title, id) => ({
    id: id ?? "conv-new", agent_id: agentId, title, turn_count: 0,
    created_at: "2026-11-12T00:00:00Z", updated_at: "2026-11-12T00:00:00Z",
  }));
  vi.mocked(listEvents).mockResolvedValue([]);
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null });
  vi.mocked(subscribeRun).mockImplementation(() => vi.fn());
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("per-chat model settings beside the composer", () => {
  it("sends no model_settings while every field is empty", async () => {
    await renderReady();
    await send();
    expect(posts[0].body).toEqual({ run_id: expect.any(String), input: "Hi" });
  });

  it("sends the typed overrides with the turn's run, as numbers, and only the ones still set", async () => {
    await renderReady();
    setSetting(/^Temperature/, "0.4");
    setSetting(/^Top P/, "0.9");
    setSetting(/^Max steps/, "5");
    setSetting(/^Seed/, "7");
    setSetting(/^Seed/, ""); // emptied again: unset
    await send("one");
    expect(posts[0].url).toMatch(/\/api\/v1\/conversations\/[^/]+\/runs$/);
    expect(posts[0].body).toEqual({ run_id: expect.any(String), input: "one", model_settings: { temperature: 0.4, top_p: 0.9, max_steps: 5 } });
  });

  it("does not send while a value is out of range, keeps the text and says which field", async () => {
    await renderReady();
    setSetting(/^Top P/, "2");
    expect(within(screen.getByTestId("model-settings-popover")).getByLabelText(/^Top P/)).toHaveAccessibleDescription(expect.stringContaining("Top P must be 0 to 1"));
    fireEvent.change(composer(), { target: { value: "keep me" } });
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(posts).toHaveLength(0);
    setSetting(/^Top P/, "1");
    await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
  });

  it("shows the API's refusal of a setting next to the settings button", async () => {
    await renderReady();
    setSetting(/^Top K/, "5");
    refusals.push({ status: 422, detail: "model 'openai:gpt-4.1-mini' does not accept a top_k setting; remove top_k from the run's model settings or choose another model" });
    await send();
    expect(await screen.findByTestId("model-settings-error")).toHaveTextContent("does not accept a top_k setting");
    expect(settingsButton()).toHaveAccessibleName(/\(problems\)/);
  });
});
