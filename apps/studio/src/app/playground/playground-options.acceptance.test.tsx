// P0.19 acceptance: playground run options, driven through the page with the real turn API (fetch
// is stubbed, so the POST bodies asserted here are the exact JSON sent). Complements page.test.tsx.
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlaygroundPage from "./page";
import { api } from "@/lib/api";
import { createConversation, getConversation, listConversations } from "@/lib/conversations";
import { listEvents, subscribeRun, type RunEvent, type RunEventData, type SubscribeHandlers } from "@/lib/runs";
import { fetchModels, type ModelOption } from "@/lib/metrics";
import { getRequestContext, validateRequestContext } from "@/lib/requestContext";
import type { AgentSpec, AgentVersion } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  BASE_URL: "http://api.test",
  apiHeaders: (extra: HeadersInit = {}) => extra,
  api: { agents: { list: vi.fn() }, versions: { list: vi.fn() } },
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
const specOf = (name: string, extra: object = {}): AgentSpec =>
  ({ spec_version: 1, name, instructions: "Help", model: MODEL, tools: [], ...extra }) as AgentSpec;
const versionOf = (agentId: string, version: number, extra: object = {}): AgentVersion => ({
  agent_id: agentId, version, spec: specOf(agentId, extra), spec_hash: `h${version}`,
  created_at: "2026-11-12T00:00:00Z", created_by: "dev",
});
const makeAgents = (draftExtra: object = {}) => [
  { id: "agent-1", name: "Support assistant", draft_spec: specOf("s", draftExtra), latest_version: 1 },
  { id: "agent-2", name: "Draft helper", draft_spec: specOf("d"), latest_version: null },
  { id: "agent-3", name: "Published helper", draft_spec: specOf("p"), latest_version: 2 },
];
const versionsByAgent: Record<string, AgentVersion[]> = {
  "agent-1": [versionOf("agent-1", 1)],
  "agent-3": [versionOf("agent-3", 1), versionOf("agent-3", 2)],
};
const modelList: ModelOption[] = [
  { id: "openai:gpt-4.1-mini", provider: "openai", name: "gpt-4.1-mini", wildcard: false, source: "allowlist", description: null },
  { id: "openai:gpt-4.1", provider: "openai", name: "gpt-4.1", wildcard: false, source: "allowlist", description: null },
  { id: "anthropic:*", provider: "anthropic", name: "*", wildcard: true, source: "allowlist", description: null },
  { id: "test:echo", provider: "test", name: "echo", wildcard: false, source: "scripted", description: null },
];

const event = (seq: number, type: RunEvent["type"], data: unknown): RunEvent =>
  ({ seq, type, data: data as RunEventData, ts: "2026-11-12T00:00:00Z" });

let handlers: SubscribeHandlers;
let posts: Array<{ url: string; body: Record<string, unknown> }>;
// Queued responses for the next turn POSTs; empty means "accept".
let refusals: Array<{ status: number; detail: unknown } | "network">;

const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
  const body = JSON.parse(String(init.body));
  posts.push({ url, body });
  const refusal = refusals.shift();
  if (refusal === "network") throw new TypeError("network down");
  if (refusal) {
    return { ok: false, status: refusal.status, json: async () => ({ detail: refusal.detail }) };
  }
  const m = body.model as { provider: string; name: string } | undefined;
  return {
    ok: true,
    status: 200,
    json: async () => ({
      run_id: body.run_id, status: "queued", display_status: "queued", agent_id: "agent-1",
      version: body.version === "draft" ? null : (body.version ?? 1), draft: body.version === "draft",
      model_id: m ? `${m.provider}:${m.name}` : "openai:gpt-4.1-mini", model_overridden: Boolean(m),
      conversation_id: "conv-new", turn: posts.length,
    }),
  };
});

const composer = () => screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
const choose = (name: string, value: string) =>
  fireEvent.change(screen.getByRole("combobox", { name }), { target: { value } });
const lastBody = () => posts.at(-1)?.body;

async function renderReady() {
  const view = render(<PlaygroundPage />);
  await screen.findByRole("option", { name: "Support assistant" });
  await screen.findByRole("option", { name: "openai:gpt-4.1" });
  if (window.location.search.includes("agent-3")) await screen.findByRole("option", { name: "v2" });
  return view;
}

async function send(text = "Hi") {
  const before = posts.length;
  fireEvent.change(composer(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(posts.length).toBe(before + 1));
}

/** Events of one answered turn; `requests` are [model, inTokens, outTokens, cost|null] per model request. */
function finish(requests: Array<[string, number, number, string | null]>, text = "done") {
  act(() => {
    handlers.onEvent?.(event(0, "run_started", { input: "x" }));
    requests.forEach(([model, i, o, cost], n) =>
      handlers.onEvent?.(event(n + 1, "model_message", {
        model_request: n, is_final: true, text: `${text} ${n}`, model_id: model,
        usage: { input_tokens: i, output_tokens: o }, cost_usd: cost,
      })));
    handlers.onEvent?.(event(requests.length + 1, "run_completed", { output: text }));
  });
}

const tierInfo = (agentId: string, version: unknown) => ({
  agent_id: agentId, version: typeof version === "number" ? version : null, draft: version === "draft",
  fields: [{ name: "tier", type: "string" as const, required: false, description: "Tier", max_length: 20, values: [] }],
  schema: { type: "object" as const, properties: { tier: { type: "string" as const, title: "tier" } } },
});
const ctxKey = (agentId: string) => `eap.playground.context.v1:anon:${agentId}`;

beforeEach(() => {
  window.history.replaceState({}, "", "/playground");
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  posts = [];
  refusals = [];
  vi.stubGlobal("fetch", fetchMock);
  vi.mocked(api.agents.list).mockResolvedValue(makeAgents());
  vi.mocked(api.versions.list).mockImplementation(async (id) => versionsByAgent[id] ?? []);
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
  vi.mocked(subscribeRun).mockImplementation((_id, next) => { handlers = next; return vi.fn(); });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("options sent on each turn (exact POST body)", () => {
  it("posts only run_id and input for the default model, latest version and no context", async () => {
    await renderReady();
    await send("Hello");
    expect(posts[0].url).toMatch(/\/api\/v1\/conversations\/[^/]+\/runs$/);
    expect(posts[0].body).toEqual({ run_id: expect.any(String), input: "Hello" });
    expect(Object.keys(posts[0].body).sort()).toEqual(["input", "run_id"]);
  });

  const cases: Array<[string, Record<string, string>, Record<string, unknown>]> = [
    ["an allowlisted override", { Model: "openai:gpt-4.1" }, { model: { provider: "openai", name: "gpt-4.1" } }],
    ["a scripted test model", { Model: "test:echo" }, { model: { provider: "test", name: "echo" } }],
    ["version v2 explicitly", { Version: "v2" }, { version: 2 }],
    ["version v1 (older than latest)", { Version: "v1" }, { version: 1 }],
    ["latest (version omitted)", { Version: "latest" }, {}],
    ["the draft", { Version: "draft" }, { version: "draft" }],
    ["draft plus override", { Version: "draft", Model: "test:echo" }, { version: "draft", model: { provider: "test", name: "echo" } }],
  ];
  it.each(cases)("sends %s", async (_label, picks, extra) => {
    window.history.replaceState({}, "", "/playground?agent=agent-3");
    await renderReady();
    for (const [name, value] of Object.entries(picks)) choose(name, value);
    await send();
    expect(lastBody()).toEqual({ run_id: expect.any(String), input: "Hi", ...extra });
  });

  it("sends a wildcard provider with the free-text name, trimmed", async () => {
    await renderReady();
    choose("Model", "anthropic:*");
    fireEvent.change(screen.getByRole("textbox", { name: "Model name" }), { target: { value: "  claude-x-1  " } });
    await waitFor(() => expect(composer()).toBeEnabled());
    await send();
    expect(lastBody()?.model).toEqual({ provider: "anthropic", name: "claude-x-1" });
  });

  it("does not send an override when the picked model equals the version's own", async () => {
    window.history.replaceState({}, "", "/playground?model=openai:gpt-4.1");
    await renderReady();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("openai:gpt-4.1"));
    choose("Model", "");
    await send();
    expect(lastBody()).not.toHaveProperty("model");
  });

  it("omits the context when fields exist but no value was entered", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (id, v) => tierInfo(id, v));
    await renderReady();
    await screen.findByRole("button", { name: /0 of 1 set/ });
    await send();
    expect(lastBody()).not.toHaveProperty("request_context");
  });

  it("sends only the non-empty remembered context values", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (id, v) => tierInfo(id, v));
    window.sessionStorage.setItem(ctxKey("agent-1"), JSON.stringify({ value: { tier: "gold", blank: "" } }));
    await renderReady();
    await screen.findByRole("button", { name: /1 of 1 set/ });
    await send();
    expect(lastBody()?.request_context).toEqual({ tier: "gold" });
  });

  it("sends no context when the version has no fields, even with a remembered value", async () => {
    window.sessionStorage.setItem(ctxKey("agent-1"), JSON.stringify({ value: { stale: "x" } }));
    await renderReady();
    await screen.findByRole("button", { name: /No context fields/ });
    await send();
    expect(lastBody()).not.toHaveProperty("request_context");
  });

  it("re-reads the options on every turn of one conversation", async () => {
    window.history.replaceState({}, "", "/playground?agent=agent-3");
    await renderReady();
    choose("Version", "v1");
    await send("one");
    finish([["openai:gpt-4.1-mini", 1, 1, "0.001"]]);
    await waitFor(() => expect(composer()).toBeEnabled());
    choose("Version", "draft");
    choose("Model", "openai:gpt-4.1");
    await send("two");
    expect(posts.map((p) => p.url).filter((u, i, a) => a.indexOf(u) === i)).toHaveLength(1);
    expect(posts[0].body).toMatchObject({ input: "one", version: 1 });
    expect(posts[0].body).not.toHaveProperty("model");
    expect(posts[1].body).toMatchObject({ input: "two", version: "draft", model: { provider: "openai", name: "gpt-4.1" } });
    expect(posts[0].body.run_id).not.toBe(posts[1].body.run_id);
  });
});

const texts = (id: string) => screen.getAllByTestId(id).map((el) => el.textContent);
const hrefs = () => screen.getAllByRole("link", { name: "Open trace" }).map((el) => el.getAttribute("href"));

describe("acceptance: two turns of one conversation, two models", () => {
  it("shows each turn's own model, tokens, cost, version and the override pill on the second only", async () => {
    await renderReady();
    await send("first");
    const run1 = posts[0].body.run_id;
    finish([["openai:gpt-4.1-mini", 1200, 300, "0.0012"]], "alpha");
    await waitFor(() => expect(composer()).toBeEnabled());
    choose("Model", "openai:gpt-4.1");
    await send("second");
    const run2 = posts[1].body.run_id;
    finish([["openai:gpt-4.1", 4000, 500, "0.02"]], "beta");

    await waitFor(() => expect(screen.getAllByTestId("turn-badge")).toHaveLength(2));
    expect(texts("turn-badge-model")).toEqual(["openai:gpt-4.1-mini", "openai:gpt-4.1"]);
    expect(texts("turn-badge-tokens")).toEqual(["1,200 in · 300 out tokens", "4,000 in · 500 out tokens"]);
    expect(texts("turn-badge-cost")).toEqual(["$0.0012", "$0.02"]);
    expect(texts("turn-badge-version")).toEqual(["v1", "v1"]);
    const badges = screen.getAllByTestId("turn-badge");
    expect(within(badges[0]).queryByText("override")).not.toBeInTheDocument();
    expect(within(badges[1]).getByText("override")).toBeInTheDocument();
    expect(hrefs()).toEqual([`/traces/${run1}`, `/traces/${run2}`]);
    expect(screen.getByText("alpha 0")).toBeInTheDocument();
    expect(screen.getByText("beta 0")).toBeInTheDocument();
  });

  it("sums event usage over a turn's model requests when the run has no totals", async () => {
    await renderReady();
    await send();
    finish([["openai:gpt-4.1-mini", 100, 10, "0.001"], ["openai:gpt-4.1-mini", 200, 20, "0.002"]]);
    await waitFor(() => expect(screen.getByTestId("turn-badge-cost")).toBeInTheDocument());
    expect(screen.getByTestId("turn-badge-tokens")).toHaveTextContent("300 in · 30 out tokens");
    expect(screen.getByTestId("turn-badge-cost")).toHaveTextContent("$0.003");
  });

  it("shows 'no price' when any request in the turn was unpriced", async () => {
    await renderReady();
    await send();
    finish([["openai:gpt-4.1-mini", 100, 10, "0.001"], ["openai:gpt-4.1-mini", 5, 5, null]]);
    await waitFor(() => expect(screen.getByTestId("turn-badge-cost")).toHaveTextContent("no price"));
    expect(screen.getByTestId("turn-badge-tokens")).toHaveTextContent("105 in · 15 out tokens");
  });

  it("shows pending tokens while running and 'usage not recorded' for a finished turn without usage", async () => {
    await renderReady();
    await send();
    expect(screen.getByTestId("turn-badge-model")).toHaveTextContent("openai:gpt-4.1-mini");
    expect(screen.getByTestId("turn-badge-tokens")).toHaveTextContent("tokens pending");
    expect(screen.queryByTestId("turn-badge-cost")).not.toBeInTheDocument();
    act(() => {
      handlers.onEvent?.(event(0, "run_started", { input: "x" }));
      handlers.onEvent?.(event(1, "model_message", { model_request: 0, is_final: true, text: "ok" }));
      handlers.onEvent?.(event(2, "run_completed", { output: "ok" }));
    });
    await waitFor(() => expect(screen.getByTestId("turn-badge-tokens")).toHaveTextContent("usage not recorded"));
  });

  it("prefers the run's recorded totals over event usage after a reload, and links each trace", async () => {
    const base = { agent_id: "agent-3", version: 1, output: null, error: null, heartbeat_at: null,
      created_at: "2026-11-12T00:00:00Z", updated_at: "2026-11-12T00:00:00Z", trace_id: null,
      conversation_id: "conv-1", status: "completed", display_status: "completed" };
    vi.mocked(getConversation).mockResolvedValue({
      id: "conv-1", agent_id: "agent-3", title: "t", turn_count: 2,
      created_at: base.created_at, updated_at: base.updated_at,
      runs: [
        { ...base, run_id: "r1", turn: 1, input: "a", model_id: "openai:gpt-4.1-mini", model_overridden: false,
          input_tokens: 70, output_tokens: 7, cost_usd: "0.0007000000" },
        { ...base, run_id: "r2", turn: 2, input: "b", version: 2, model_id: "openai:gpt-4.1", model_overridden: true,
          input_tokens: 9000, output_tokens: 90, cost_usd: "0.05" },
      ] as never,
    });
    const answered = (m: string) => [
      event(0, "run_started", { input: "x" }),
      event(1, "model_message", { model_request: 0, is_final: true, text: "ans", model_id: m,
        usage: { input_tokens: 1, output_tokens: 1 }, cost_usd: "9.99" }),
      event(2, "run_completed", { output: "ans" }),
    ];
    vi.mocked(listEvents).mockImplementation(async (id) => answered(id === "r1" ? "other:x" : "other:y"));
    window.history.replaceState({}, "", "/playground?conversation=conv-1&agent=agent-3");
    await renderReady();
    await waitFor(() => expect(screen.getAllByTestId("turn-badge")).toHaveLength(2));
    expect(texts("turn-badge-model")).toEqual(["openai:gpt-4.1-mini", "openai:gpt-4.1"]);
    expect(texts("turn-badge-tokens")).toEqual(["70 in · 7 out tokens", "9,000 in · 90 out tokens"]);
    expect(texts("turn-badge-cost")).toEqual(["$0.00070", "$0.05"]);
    expect(texts("turn-badge-version")).toEqual(["v1", "v2"]);
    const pills = screen.getAllByTestId("turn-badge").map((b) => within(b).queryByText("override") !== null);
    expect(pills).toEqual([false, true]);
    expect(hrefs()).toEqual(["/traces/r1", "/traces/r2"]);
  });
});

describe("422 refusals are shown inline", () => {
  const modelRefusal = "model 'openai:gpt-4.1' is not allowed on this platform (not in the EAP_ALLOWED_MODELS allowlist)";

  it("puts a string model refusal under the picker, with aria wiring, and keeps the composer usable", async () => {
    refusals.push({ status: 422, detail: modelRefusal });
    await renderReady();
    choose("Model", "openai:gpt-4.1");
    await send("keep me");
    const error = await screen.findByText(modelRefusal);
    expect(error).toHaveAttribute("id", "playground-model-error");
    const picker = screen.getByRole("combobox", { name: "Model" });
    expect(picker).toHaveAttribute("aria-invalid", "true");
    expect(picker).toHaveAttribute("aria-describedby", "playground-model-error");
    expect(screen.getByRole("combobox", { name: "Version" })).toHaveAttribute("aria-invalid", "false");
    expect(screen.getByTestId("unsent-message")).toHaveTextContent("keep me");
    // Composer is usable again, the refused turn left no footer or bubble, no top alert.
    await waitFor(() => expect(composer()).toBeEnabled());
    expect(screen.getByRole("button", { name: "Send message" })).toBeInTheDocument();
    expect(screen.queryAllByTestId("turn-footer")).toHaveLength(0);
    expect(screen.queryByText("keep me", { selector: "p" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Failed to send/)).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("openai:gpt-4.1");
  });

  it("maps a 422 list on body.model and body.version to their own controls", async () => {
    refusals.push({ status: 422, detail: [
      { loc: ["body", "model", "name"], msg: "name too long", type: "x" },
      { loc: ["body", "version"], msg: "version 9 does not exist", type: "x" },
    ] });
    await renderReady();
    await send();
    expect(await screen.findByText("name too long")).toHaveAttribute("id", "playground-model-error");
    expect(screen.getByText("version 9 does not exist")).toHaveAttribute("id", "playground-version-error");
    expect(screen.getByRole("combobox", { name: "Version" })).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("combobox", { name: "Version" })).toHaveAttribute("aria-describedby", "playground-version-error");
  });

  it("shows request_context field errors and a banner in the panel, opening it", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (id, v) => tierInfo(id, v));
    window.sessionStorage.setItem(ctxKey("agent-1"), JSON.stringify({ value: { tier: "x" } }));
    refusals.push({ status: 422, detail: [
      { loc: ["body", "request_context", "tier"], msg: "must be one of gold, silver", type: "x" },
      { loc: ["body", "request_context"], msg: "context is required", type: "x" },
    ] });
    await renderReady();
    await screen.findByRole("button", { name: /1 of 1 set/ });
    expect(screen.queryByTestId("playground-context-panel")).not.toBeInTheDocument();
    await send("ctx");
    const box = await screen.findByTestId("context-turn-errors");
    expect(box).toHaveTextContent("tier");
    expect(box).toHaveTextContent("must be one of gold, silver");
    expect(box).toHaveTextContent("context is required");
    expect(screen.getByRole("button", { name: /problems/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("unsent-message")).toHaveTextContent("ctx");
  });

  it("shows other 422s in turn-options-error", async () => {
    refusals.push({ status: 422, detail: [{ loc: ["body", "run_id"], msg: "not a uuid", type: "x" }] });
    await renderReady();
    await send();
    expect(await screen.findByTestId("turn-options-error")).toHaveTextContent("run_id: not a uuid");
    expect(screen.getByTestId("unsent-message")).toBeInTheDocument();
  });

  it("clears every field error on the next successful turn and retries with the same text", async () => {
    refusals.push({ status: 422, detail: modelRefusal });
    await renderReady();
    choose("Model", "openai:gpt-4.1");
    await send("retry me");
    await screen.findByText(modelRefusal);
    await waitFor(() => expect(composer()).toBeEnabled());
    await send("retry me");
    expect(posts).toHaveLength(2);
    expect(posts[1].body).toMatchObject({ input: "retry me", model: { provider: "openai", name: "gpt-4.1" } });
    await waitFor(() => expect(screen.queryByText(modelRefusal)).not.toBeInTheDocument());
    expect(screen.queryByTestId("unsent-message")).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveAttribute("aria-invalid", "false");
    expect(screen.getAllByTestId("turn-footer")).toHaveLength(1);
  });

  it("clears a version error once the version is changed, without touching the model error", async () => {
    refusals.push({ status: 422, detail: [
      { loc: ["body", "version"], msg: "bad version", type: "x" },
      { loc: ["body", "model"], msg: "bad model", type: "x" },
    ] });
    await renderReady();
    await send();
    await screen.findByText("bad version");
    choose("Version", "draft");
    expect(screen.queryByText("bad version")).not.toBeInTheDocument();
    expect(screen.getByText("bad model")).toBeInTheDocument();
  });
});

describe("failures other than 422 use the top alert", () => {
  it.each([
    ["a 500 with a string detail", { status: 500, detail: "database unavailable" }, "Failed to send the message: database unavailable"],
    ["a 409 conflict", { status: 409, detail: "previous turn still running" }, "Failed to send the message: previous turn still running"],
    ["a 500 without a body", { status: 500, detail: null }, "Failed to send the message: HTTP 500"],
    ["a network error", "network" as const, "Failed to send the message."],
  ])("shows %s as one alert, not inline", async (_l, refusal, message) => {
    refusals.push(refusal);
    await renderReady();
    await send("boom");
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    for (const id of ["turn-options-error", "unsent-message", "context-turn-errors"]) {
      expect(screen.queryByTestId(id)).not.toBeInTheDocument();
    }
    expect(document.getElementById("playground-model-error")).toBeNull();
    expect(screen.queryAllByTestId("turn-footer")).toHaveLength(0);
    await waitFor(() => expect(composer()).toBeEnabled());
  });
});

describe("reload keeps the options", () => {
  const baseRun = { agent_id: "agent-3", output: null, error: null, heartbeat_at: null,
    created_at: "2026-11-12T00:00:00Z", updated_at: "2026-11-12T00:00:00Z", trace_id: null, conversation_id: "conv-1" };

  it("restores agent, version, model and context after the page is remounted on the same URL", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (id, v) => tierInfo(id, v));
    const first = await renderReady();
    choose("Agent", "agent-3");
    await screen.findByRole("option", { name: "v1" });
    choose("Version", "v1");
    choose("Model", "test:echo");
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("model")).toBe("test:echo"));
    fireEvent.click(await screen.findByRole("button", { name: /of 1 set/ }));
    fireEvent.click(await screen.findByRole("button", { name: "JSON" }));
    fireEvent.change(screen.getByLabelText("Request context JSON"), { target: { value: '{"tier":"silver"}' } });
    await waitFor(() => expect(window.sessionStorage.getItem(ctxKey("agent-3"))).toContain("silver"));
    expect(new URLSearchParams(window.location.search).get("agent")).toBe("agent-3");
    expect(new URLSearchParams(window.location.search).get("version")).toBe("1");
    first.unmount();

    await renderReady();
    expect(screen.getByRole("combobox", { name: "Agent" })).toHaveValue("agent-3");
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Version" })).toHaveValue("v1"));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("test:echo"));
    await screen.findByRole("button", { name: /1 of 1 set/ });
    await send();
    expect(lastBody()).toMatchObject({ version: 1, model: { provider: "test", name: "echo" }, request_context: { tier: "silver" } });
  });

  it("keeps a remembered context per agent and does not leak it to another agent", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (id, v) => tierInfo(id, v));
    window.sessionStorage.setItem(ctxKey("agent-3"), JSON.stringify({ value: { tier: "gold" } }));
    window.history.replaceState({}, "", "/playground?agent=agent-1");
    await renderReady();
    await screen.findByRole("button", { name: /0 of 1 set/ });
    await send();
    expect(lastBody()).not.toHaveProperty("request_context");
  });

  it.each([
    ["an unknown wildcard provider", "mistral:large"],
    ["a model that was never listed", "openai:gpt-5"],
  ])("drops %s from the URL with a note and sends no override", async (_l, model) => {
    window.history.replaceState({}, "", `/playground?agent=agent-1&model=${model}`);
    await renderReady();
    expect(await screen.findByText(`${model} is not offered here; turns use the version's model.`)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("");
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("model")).toBeNull());
    await send();
    expect(lastBody()).not.toHaveProperty("model");
  });

  it("treats the version's own model in the URL as no override, without a note", async () => {
    window.history.replaceState({}, "", "/playground?agent=agent-1&model=openai:gpt-4.1-mini");
    await renderReady();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue(""));
    expect(screen.queryByText(/is not offered here/)).not.toBeInTheDocument();
    await send();
    expect(lastBody()).not.toHaveProperty("model");
  });

  it("falls back to latest for a version number the agent does not have", async () => {
    window.history.replaceState({}, "", "/playground?agent=agent-1&version=7");
    await renderReady();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Version" })).toHaveValue("latest"));
    await send();
    expect(lastBody()).not.toHaveProperty("version");
  });

  it("uses the preset when nothing is remembered, and the remembered value once there is one", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (id, v) => tierInfo(id, v));
    const { saveContext } = await import("@/lib/requestContext");
    saveContext("agent-1", { tier: "preset" }, null);
    const first = await renderReady();
    await screen.findByRole("button", { name: /1 of 1 set/ });
    fireEvent.click(screen.getByRole("button", { name: /1 of 1 set/ }));
    expect(screen.getByText("Using the preset saved for this agent.")).toBeInTheDocument();
    await send("p");
    expect(lastBody()?.request_context).toEqual({ tier: "preset" });
    first.unmount();
    window.sessionStorage.setItem(ctxKey("agent-1"), JSON.stringify({ value: { tier: "mine" } }));
    window.history.replaceState({}, "", "/playground?agent=agent-1");
    await renderReady();
    await screen.findByRole("button", { name: /1 of 1 set/ });
    await send("q");
    expect(lastBody()?.request_context).toEqual({ tier: "mine" });
  });

  it("re-posts a queued turn with the run's own options, not the current UI options", async () => {
    vi.mocked(getConversation).mockResolvedValue({
      id: "conv-1", agent_id: "agent-3", title: "t", turn_count: 1,
      created_at: baseRun.created_at, updated_at: baseRun.updated_at,
      runs: [{ ...baseRun, run_id: "run-q", turn: 1, input: "queued input", version: 1, status: "queued",
        display_status: "queued", model_overridden: false, model_id: "openai:gpt-4.1-mini",
        request_context: { tier: "bronze" } }] as never,
    });
    window.history.replaceState({}, "", "/playground?conversation=conv-1&agent=agent-3&version=2&model=test:echo");
    await renderReady();
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toBe("http://api.test/api/v1/conversations/conv-1/runs");
    expect(posts[0].body).toEqual({ run_id: "run-q", input: "queued input", version: 1, request_context: { tier: "bronze" } });
    await waitFor(() => expect(subscribeRun).toHaveBeenCalledWith("run-q", expect.any(Object), expect.anything()));
  });

  it("does not re-post a turn that is already running or finished", async () => {
    vi.mocked(getConversation).mockResolvedValue({
      id: "conv-1", agent_id: "agent-3", title: "t", turn_count: 1,
      created_at: baseRun.created_at, updated_at: baseRun.updated_at,
      runs: [{ ...baseRun, run_id: "run-r", turn: 1, input: "x", version: 1, status: "running", display_status: "running" }] as never,
    });
    window.history.replaceState({}, "", "/playground?conversation=conv-1&agent=agent-3");
    await renderReady();
    await waitFor(() => expect(subscribeRun).toHaveBeenCalled());
    expect(posts).toHaveLength(0);
  });
});

describe("suppressed streaming (output processors)", () => {
  const withProcessors = (n: number) => ({ output_processors: [{ type: "pii_redact" }], v: n });
  const assistantBubbles = () => document.querySelectorAll(".mr-auto.max-w-\\[80\\%\\]");

  beforeEach(() => {
    vi.mocked(api.versions.list).mockImplementation(async (id) =>
      id === "agent-1" ? [versionOf("agent-1", 1, withProcessors(1))] : (versionsByAgent[id] ?? []));
  });

  it("shows the checking state and no answer text until the turn completes, then the final message", async () => {
    await renderReady();
    await waitFor(() => expect(api.versions.list).toHaveBeenCalled());
    await send("question");
    const checking = await screen.findByTestId("turn-output-checking");
    expect(checking).toHaveAttribute("role", "status");
    act(() => { handlers.onEvent?.(event(0, "run_started", { input: "question" })); });
    expect(screen.getByTestId("turn-output-checking")).toBeInTheDocument();
    // Deltas that arrive anyway (defence in depth for P0.17) never render.
    const runId = String(lastBody()?.run_id);
    act(() => {
      handlers.onDelta?.({ run_id: runId, model_request: 0, attempt: "a", delta: "final answer UNCHECKED " });
      handlers.onDelta?.({ run_id: runId, model_request: 0, attempt: "a", delta: "jane@example.com" });
    });
    expect(screen.getByTestId("turn-output-checking")).toBeInTheDocument();
    expect(screen.getByTestId("turn-footer")).toHaveAttribute("data-live-text", "false");
    expect(screen.queryByText(/UNCHECKED|jane@example\.com/)).not.toBeInTheDocument();
    expect(screen.queryByText(/final answer/)).not.toBeInTheDocument();
    expect(Array.from(assistantBubbles()).map((b) => b.textContent)).toEqual(expect.not.arrayContaining(["final answer 0"]));
    expect(Array.from(assistantBubbles()).every((b) => (b.textContent ?? "").trim() === "")).toBe(true);
    finish([["openai:gpt-4.1-mini", 10, 5, "0.001"]], "final answer");
    expect(await screen.findByText("final answer 0")).toBeInTheDocument();
    expect(screen.queryByTestId("turn-output-checking")).not.toBeInTheDocument();
  });

  it("shows the state only on the running turn, and not on an earlier finished one", async () => {
    await renderReady();
    await waitFor(() => expect(api.versions.list).toHaveBeenCalled());
    await send("one");
    finish([["openai:gpt-4.1-mini", 1, 1, "0.001"]], "first");
    await waitFor(() => expect(composer()).toBeEnabled());
    await send("two");
    expect(await screen.findAllByTestId("turn-output-checking")).toHaveLength(1);
    expect(screen.getAllByTestId("turn-footer")).toHaveLength(2);
  });

  it("drops the state when the turn fails without a block", async () => {
    await renderReady();
    await waitFor(() => expect(api.versions.list).toHaveBeenCalled());
    await send();
    await screen.findByTestId("turn-output-checking");
    act(() => { handlers.onEvent?.(event(0, "run_failed", { status: "failed", error: "provider exploded" })); });
    await waitFor(() => expect(screen.queryByTestId("turn-output-checking")).not.toBeInTheDocument());
    expect(screen.getByText("provider exploded")).toBeInTheDocument();
  });

  it("follows the targeted spec: the draft's processors count when the draft is run, not the latest's", async () => {
    vi.mocked(api.versions.list).mockImplementation(async (id) => versionsByAgent[id] ?? []);
    vi.mocked(api.agents.list).mockResolvedValue(makeAgents(withProcessors(0)));
    await renderReady();
    await waitFor(() => expect(api.versions.list).toHaveBeenCalled());
    await send("latest");
    await waitFor(() => expect(screen.getByTestId("turn-badge")).toBeInTheDocument());
    expect(screen.queryByTestId("turn-output-checking")).not.toBeInTheDocument();
    finish([["openai:gpt-4.1-mini", 1, 1, "0.001"]]);
    await waitFor(() => expect(composer()).toBeEnabled());
    choose("Version", "draft");
    await send("draft");
    expect(await screen.findByTestId("turn-output-checking")).toBeInTheDocument();
    expect(screen.getAllByTestId("turn-footer")).toHaveLength(2);
  });
});

describe("blocked turns", () => {
  const block = (code: string, message: string) => act(() => {
    handlers.onEvent?.(event(0, "run_started", { input: "[withheld]" }));
    handlers.onEvent?.(event(1, "run_failed", { status: "failed", error: message, error_code: code }));
  });

  it("shows an input_blocked turn as one alert under its message, not as an answer", async () => {
    await renderReady();
    await send("my card is 4111");
    block("input_blocked", "the message was blocked by this agent's input processors");
    const blocked = await screen.findByTestId("turn-blocked");
    expect(blocked).toHaveAttribute("data-code", "input_blocked");
    expect(blocked).toHaveTextContent("Message blocked");
    expect(blocked).toHaveTextContent("The message was blocked by this agent's input processors.");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.queryByText("the message was blocked by this agent's input processors")).not.toBeInTheDocument();
    const bubbles = Array.from(document.querySelectorAll(".mr-auto.max-w-\\[80\\%\\]"));
    expect(bubbles.every((b) => (b.textContent ?? "").trim() === "")).toBe(true);
    await waitFor(() => expect(composer()).toBeEnabled());
  });

  it("falls back to a fixed sentence when a block has no message", async () => {
    await renderReady();
    await send();
    act(() => {
      handlers.onEvent?.(event(0, "run_started", { input: "x" }));
      handlers.onEvent?.(event(1, "run_failed", { status: "failed", error_code: "output_blocked" }));
    });
    expect(await screen.findByTestId("turn-blocked")).toHaveTextContent("The answer was blocked by this agent's output processors.");
  });

  it("keeps an earlier answer intact and shows exactly one alert for the blocked latest turn", async () => {
    await renderReady();
    await send("one");
    finish([["openai:gpt-4.1-mini", 10, 5, "0.001"]], "good answer");
    await waitFor(() => expect(composer()).toBeEnabled());
    await send("two");
    block("output_blocked", "the answer was blocked by this agent's output processors");
    await screen.findByTestId("turn-blocked");
    expect(screen.getByTestId("turn-blocked")).toHaveAttribute("data-code", "output_blocked");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByText("good answer 0")).toBeInTheDocument();
    expect(screen.getAllByTestId("turn-footer")).toHaveLength(2);
    // The next turn goes through; the old block stays under its own turn, still the only alert.
    await waitFor(() => expect(composer()).toBeEnabled());
    await send("three");
    expect(posts).toHaveLength(3);
    expect(screen.getAllByTestId("turn-blocked")).toHaveLength(1);
    // Only the turn that just became blocked is announced; once the next turn starts it is not.
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
    expect(screen.getByTestId("turn-blocked")).not.toHaveAttribute("role");
  });

  it("shows a non-block failure in the top alert, with no block notice", async () => {
    await renderReady();
    await send();
    act(() => {
      handlers.onEvent?.(event(0, "run_started", { input: "x" }));
      handlers.onEvent?.(event(1, "run_failed", { status: "failed", error: "model timed out" }));
    });
    expect((await screen.findAllByText("model timed out")).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("turn-blocked")).not.toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("displays a loaded conversation whose latest turn is blocked, without announcing it", async () => {
    const base = { agent_id: "agent-3", version: 1, output: null, heartbeat_at: null, trace_id: null, conversation_id: "conv-1",
      created_at: "2026-11-12T00:00:00Z", updated_at: "2026-11-12T00:00:00Z" };
    vi.mocked(getConversation).mockResolvedValue({
      id: "conv-1", agent_id: "agent-3", title: "t", turn_count: 2, created_at: base.created_at, updated_at: base.updated_at,
      runs: [
        { ...base, run_id: "a", turn: 1, input: "hi", status: "completed", display_status: "completed", error: null },
        { ...base, run_id: "b", turn: 2, input: "bad", status: "failed", display_status: "failed", error: "blocked by input processors" },
      ] as never,
    });
    vi.mocked(listEvents).mockImplementation(async (id) => id === "a"
      ? [event(0, "run_started", { input: "hi" }), event(1, "model_message", { model_request: 0, is_final: true, text: "hello" }),
         event(2, "run_completed", { output: "hello" })]
      : [event(0, "run_started", { input: "[withheld]" }),
         event(1, "run_failed", { status: "failed", error: "blocked by input processors", error_code: "input_blocked" })]);
    window.history.replaceState({}, "", "/playground?conversation=conv-1&agent=agent-3");
    await renderReady();
    expect(await screen.findByTestId("turn-blocked")).toHaveAttribute("data-code", "input_blocked");
    expect(screen.getByText("hello")).toBeInTheDocument();
    // A block found on reload is displayed, not announced.
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
    expect(screen.getByTestId("turn-blocked")).not.toHaveAttribute("role");
  });
});

describe("draft-only agents", () => {
  const pickDraftAgent = async () => {
    window.history.replaceState({}, "", "/playground?agent=agent-2");
    await renderReady();
  };

  it("labels the agent and offers only the draft, selected", async () => {
    await pickDraftAgent();
    expect(screen.getByRole("option", { name: "Draft helper (draft only)" })).toBeInTheDocument();
    const version = screen.getByRole("combobox", { name: "Version" });
    expect(version).toHaveValue("draft");
    expect(within(version).getAllByRole("option").map((o) => o.textContent)).toEqual(["Draft (unpublished)"]);
  });

  it("runs the draft on every turn, even when the URL asks for a version", async () => {
    window.history.replaceState({}, "", "/playground?agent=agent-2&version=3");
    await renderReady();
    expect(screen.getByRole("combobox", { name: "Version" })).toHaveValue("draft");
    await send("one");
    finish([["openai:gpt-4.1-mini", 1, 1, "0.001"]]);
    await waitFor(() => expect(composer()).toBeEnabled());
    await send("two");
    expect(posts.map((p) => p.body.version)).toEqual(["draft", "draft"]);
    expect(await screen.findAllByTestId("turn-badge-version")).toHaveLength(2);
    texts("turn-badge-version").forEach((t) => expect(t).toBe("Draft"));
    expect(new URLSearchParams(window.location.search).get("version")).toBe("draft");
  });
});

describe("localStorage unavailable", () => {
  it("still works when every Storage call throws", async () => {
    vi.mocked(getRequestContext).mockImplementation(async (id, v) => tierInfo(id, v));
    const boom = () => { throw new Error("storage disabled"); };
    const spies = [
      vi.spyOn(Storage.prototype, "getItem").mockImplementation(boom),
      vi.spyOn(Storage.prototype, "setItem").mockImplementation(boom),
    ];
    try {
      await renderReady();
      choose("Model", "openai:gpt-4.1");
      fireEvent.click(await screen.findByRole("button", { name: /of 1 set/ }));
      fireEvent.click(await screen.findByRole("button", { name: "JSON" }));
      fireEvent.change(screen.getByLabelText("Request context JSON"), { target: { value: '{"tier":"gold"}' } });
      await screen.findByRole("button", { name: /1 of 1 set/ });
      await send("works");
      expect(lastBody()).toMatchObject({ input: "works", model: { provider: "openai", name: "gpt-4.1" }, request_context: { tier: "gold" } });
      expect(screen.queryByText("Failed to load agents.")).not.toBeInTheDocument();
      finish([["openai:gpt-4.1", 1, 1, "0.001"]]);
      expect(await screen.findByTestId("turn-badge-model")).toHaveTextContent("openai:gpt-4.1");
    } finally {
      spies.forEach((s) => s.mockRestore());
    }
  });

  it("still works when reading window.localStorage itself throws", async () => {
    const original = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", { configurable: true, get() { throw new Error("denied"); } });
    try {
      await renderReady();
      await send("hi");
      expect(lastBody()).toEqual({ run_id: expect.any(String), input: "hi" });
      expect(await screen.findByTestId("turn-badge")).toBeInTheDocument();
    } finally {
      if (original) Object.defineProperty(window, "localStorage", original);
      else delete (window as unknown as Record<string, unknown>).localStorage;
    }
  });
});

describe("accessibility", () => {
  it("labels every control and starts with no invalid state", async () => {
    await renderReady();
    for (const name of ["Agent", "Version", "Model"]) {
      expect(screen.getByRole("combobox", { name })).toBeInTheDocument();
    }
    expect(screen.getByLabelText("Version")).toBe(screen.getByRole("combobox", { name: "Version" }));
    expect(screen.getByLabelText("Model")).toBe(screen.getByRole("combobox", { name: "Model" }));
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveAttribute("aria-invalid", "false");
    expect(screen.getByRole("combobox", { name: "Version" })).toHaveAttribute("aria-invalid", "false");
    expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveAttribute("aria-describedby");
    choose("Model", "anthropic:*");
    expect(screen.getByRole("textbox", { name: "Model name" })).toBeInTheDocument();
  });

  it("marks a wildcard without a name invalid and announces it", async () => {
    await renderReady();
    choose("Model", "anthropic:*");
    const picker = screen.getByRole("combobox", { name: "Model" });
    expect(picker).toHaveAttribute("aria-invalid", "true");
    expect(picker).toHaveAttribute("aria-describedby", "playground-model-error");
    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("id", "playground-model-error");
    expect(alert).toHaveTextContent("Enter a model name for anthropic");
  });

  it("exposes the context toggle as an expandable button controlling a labelled region", async () => {
    await renderReady();
    const toggle = await screen.findByRole("button", { name: /No context fields/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "playground-context-panel");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const region = screen.getByRole("region", { name: "Request context" });
    expect(region.id).toBe("playground-context-panel");
  });

  it("keeps all controls reachable by keyboard, in order", async () => {
    await renderReady();
    await screen.findByRole("button", { name: /No context fields/ });
    const controls = [
      screen.getByRole("combobox", { name: "Agent" }),
      screen.getByRole("combobox", { name: "Version" }),
      screen.getByRole("combobox", { name: "Model" }),
      screen.getByRole("button", { name: /No context fields/ }),
      composer(),
    ];
    for (const el of controls) {
      expect(el).not.toBeDisabled();
      expect(el.tabIndex).toBeGreaterThanOrEqual(0);
    }
    for (let i = 1; i < controls.length; i += 1) {
      expect(controls[i - 1].compareDocumentPosition(controls[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    controls[3].focus();
    expect(document.activeElement).toBe(controls[3]);
  });

  it("marks the model picker invalid and linked to its refusal after a 422", async () => {
    refusals.push({ status: 422, detail: "model 'openai:gpt-4.1' is not allowed on this platform" });
    await renderReady();
    choose("Model", "openai:gpt-4.1");
    await send();
    const alert = await screen.findByText(/is not allowed on this platform/);
    expect(alert).toHaveAttribute("role", "alert");
    const picker = screen.getByRole("combobox", { name: "Model" });
    expect(picker).toHaveAttribute("aria-invalid", "true");
    expect(document.getElementById(picker.getAttribute("aria-describedby") ?? "")).toBe(alert);
  });
});
