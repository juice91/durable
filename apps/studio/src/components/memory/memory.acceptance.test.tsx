/**
 * RM.18 acceptance: the memory inspector end to end. Complements memory.test.tsx, MemoryTab.test.tsx
 * and agentPage/chatMemory.test.tsx (kept as they are): the If-Match flow, the observation rules as
 * the API has them, the post-turn poll, the Memory tab, Act as user, Delete conversation, the
 * unsaved-edit confirms, layout and axe. Fetch is stubbed for the memory routes; the chat's own
 * modules are mocked as in the chat acceptance suites.
 */
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ path: "/agents/a1/chat", search: "", push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => ({ id: "a1" }),
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
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

import { api, type Agent, type AgentSpec, type AgentVersion } from "@/lib/api";
import { createConversation, deleteConversation, getConversation, listConversations } from "@/lib/conversations";
import { fetchModels } from "@/lib/metrics";
import { getRequestContext } from "@/lib/requestContext";
import { listEvents, subscribeRun } from "@/lib/runs";
import { startTurnWithOptions } from "@/components/chat/turnApi";
import { routes } from "@/lib/routes";
import { credentialScope } from "@/lib/requestContext";
import {
  POST_TURN_POLL_MS,
  POST_TURN_TIMEOUT_MS,
  blockedByExpired,
  errorStatus,
  memoryWrites,
  newestObservation,
  parseNoteText,
  postTurnReported,
  recallSummary,
  resourceIdProblem,
  type Observation,
} from "@/lib/memory";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { FrameHarness, mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH, type MatchMediaControl } from "@/components/frame/frameTesting";
import { KeyboardHarness, pressKey, pressKeys } from "@/components/shortcuts/shortcutsTesting";
import { AgentLayout } from "@/components/agentPage/AgentLayout";
import { ChatTab } from "@/components/agentPage/ChatTab";
import { ComposerActAsChip } from "@/components/agentPage/ComposerActAsChip";
import { MemoryCard } from "@/components/agentPage/MemoryCard";
import { WorkingMemoryEditor } from "./WorkingMemoryEditor";
import { ObservationsList } from "./ObservationsList";
import { MemoryTab } from "./MemoryTab";
import { useMemoryActivity, type LatestTurn } from "./useMemoryActivity";
import { ACT_AS_PREFIX, MEMORY_CARD_KEY } from "./memoryModel";

// --------------------------------------------------------------------------------- a fake API

type Call = { url: URL; path: string; method: string; headers: Headers; body: unknown };
let calls: Call[];
/** Overrides: return a Response to take over a request, or undefined to fall through to the fake. */
let override: ((call: Call) => Response | undefined) | null;
const T0 = "2026-11-14T10:00:00Z";

const json = (body: unknown, status = 200): Response =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

interface FakeNote {
  content: string | Record<string, unknown>;
  version: number;
}
/** Working memory notes by API path, with the API's If-Match rules (routes/memory.py). */
let notes: Map<string, FakeNote>;
let observations: Record<string, Observation[]>;
/** Conversations of the user with a queued or running turn: a forget answers 409 while any. */
let busyConversations: string[];

const noteBody = (path: string, note: FakeNote) => {
  const resource = path.match(/\/memory\/resources\/([^/]+)\/working$/);
  return {
    agent_id: "a1", scope: resource ? "resource" : "conversation", scope_key: resource ? decodeURIComponent(resource[1]) : path.split("/")[4],
    format: typeof note.content === "string" ? "markdown" : "json", content: note.content, version: note.version,
    updated_by_run_id: null, created_at: T0, updated_at: T0,
  };
};
const ifMatch = (headers: Headers): number | null => {
  const raw = headers.get("If-Match");
  return raw === null ? null : Number(raw.replace(/^W\//, "").replace(/"/g, ""));
};
const conflict = (actual: number, expected: number) =>
  json({ detail: `${actual === 0 ? "working memory does not exist (version 0)" : `working memory is at version ${actual}`}, not the If-Match version ${expected}; reload it and apply the edit again` }, 409);

/** The API's rule: the newest active row (highest covers_to_turn, markers included) only. */
const apiAcceptsDelete = (rows: Observation[], id: string): boolean => {
  const active = rows.filter((row) => !row.superseded_by);
  const top = Math.max(...active.map((row) => row.covers_to_turn));
  return active.some((row) => row.id === id && row.covers_to_turn === top);
};

function fake(call: Call): Response {
  const { path, method } = call;
  if (/\/working$/.test(path) && path !== "/api/v1/agents/a1/memory/working") {
    const note = notes.get(path);
    const expected = ifMatch(call.headers);
    if (method === "GET") return note ? json(noteBody(path, note)) : json({ detail: "Working memory not found" }, 404);
    if (method === "PUT") {
      const actual = note?.version ?? 0;
      if (expected !== null && expected !== actual) return conflict(actual, expected);
      const saved = { content: (call.body as { content: FakeNote["content"] }).content, version: actual + 1 };
      notes.set(path, saved);
      return json(noteBody(path, saved));
    }
    if (method === "DELETE") {
      if (note && expected !== null && expected !== note.version) return conflict(note.version, expected);
      notes.delete(path);
      return json(null, 204);
    }
  }
  const obs = path.match(/^\/api\/v1\/conversations\/([^/]+)\/memory\/observations(?:\/([^/]+))?$/);
  if (obs) {
    const rows = observations[obs[1]] ?? [];
    const active = rows.filter((row) => !row.superseded_by);
    if (method === "GET") return json({ items: rows, covered_to_turn: Math.max(0, ...active.map((row) => row.covers_to_turn)) });
    if (method === "DELETE" && !obs[2]) {
      observations[obs[1]] = [];
      return json({ deleted: rows.length });
    }
    if (method === "DELETE") {
      if (!rows.some((row) => row.id === obs[2])) return json({ detail: "Observation not found" }, 404);
      if (!apiAcceptsDelete(rows, obs[2])) {
        return json({ detail: "only the newest active observation (expired ones included) can be deleted (deleting an older one would hide its turns from the model); delete the newer ones first, or delete all observations" }, 409);
      }
      observations[obs[1]] = rows.filter((row) => row.id !== obs[2]);
      return json(null, 204);
    }
  }
  if (method === "GET" && path === "/api/v1/agents/a1/memory/working") {
    const items = [...notes].filter(([key]) => /\/memory\/resources\/[^/]+\/working$/.test(key)).map(([key, note]) => noteBody(key, note));
    return json({ items });
  }
  const forget = path.match(/^\/api\/v1\/(?:agents\/([^/]+)\/)?memory\/resources\/([^/]+)$/);
  if (forget && method === "DELETE") {
    if (busyConversations.length > 0) {
      return json({ detail: `a turn is still queued or running in ${busyConversations.length} conversation(s) of this resource; nothing was deleted. Retry when the turns finish`, busy_conversations: busyConversations }, 409);
    }
    const rid = decodeURIComponent(forget[2]);
    let removed = 0;
    for (const key of [...notes.keys()]) {
      if (key.includes(`/memory/resources/${forget[2]}/working`) && (!forget[1] || key.includes(`/agents/${forget[1]}/`))) {
        notes.delete(key);
        removed += 1;
      }
    }
    return json({ resource_id: rid, agent_id: forget[1] ?? null, working_memories_deleted: removed, message_embeddings_deleted: 3, conversations_deleted: call.url.searchParams.get("include_conversations") === "true" ? ["c1", "c2"] : [], busy_conversations: [] });
  }
  return json({ detail: "Not found" }, 404);
}

const row = (id: string, from: number, to: number, extra: Partial<Observation> = {}): Observation => ({
  id, agent_id: "a1", conversation_id: "c1", kind: "observation", covers_from_turn: from, covers_to_turn: to,
  content: `Notes ${id}`, approx_tokens: 40, run_id: "r1", superseded_by: null, created_at: T0, expired: false, ...extra,
});
const marker = (id: string, from: number, to: number, extra: Partial<Observation> = {}) => row(id, from, to, { expired: true, content: "", approx_tokens: 0, ...extra });
const NOTE_PATH = "/api/v1/conversations/c1/memory/working";
const USER_PATH = "/api/v1/agents/a1/memory/resources/alice/working";
const target = { scope: "conversation" as const, conversationId: "c1" };

// ------------------------------------------------------------------------------------- setup

const specOf = (memory?: object): AgentSpec =>
  ({ spec_version: 1, name: "Support bot", instructions: "Help.", model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [], ...(memory ? { memory } : {}) }) as AgentSpec;
const RESOURCE_WM = { working: { scope: "resource", template: "# User\n- Name:" } };
const FULL = { working: { scope: "conversation" }, observational: { model: { provider: "test", name: "observer" } }, semantic_recall: { top_k: 3 } };

function setAgent(draft: AgentSpec, published: AgentSpec | null = draft) {
  const agent: Agent = { id: "a1", name: "Support bot", draft_spec: draft, latest_version: published ? 1 : null };
  const versions: AgentVersion[] = published ? [{ agent_id: "a1", version: 1, spec: published, spec_hash: "h1", created_at: T0, created_by: "dev" }] : [];
  vi.mocked(api.agents.get).mockResolvedValue(agent);
  vi.mocked(api.agents.list).mockResolvedValue([agent]);
  vi.mocked(api.versions.list).mockResolvedValue(versions);
}
const conversation = (id: string, resourceId: string | null, extra: object = {}) => ({
  id, agent_id: "a1", resource_id: resourceId, title: `Chat ${id}`, turn_count: 1, last_turn_run_id: null, last_turn_status: "completed",
  last_turn_display_status: "completed", created_at: T0, updated_at: T0, ...extra,
});
let media: MatchMediaControl | undefined;
let restoreWidth: () => void = () => undefined;

beforeEach(() => {
  vi.clearAllMocks();
  calls = [];
  override = null;
  notes = new Map();
  observations = {};
  busyConversations = [];
  nav.path = "/agents/a1/chat";
  nav.search = "";
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState({}, "", routes.agentChat("a1"));
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      const call: Call = { url, path: url.pathname, method: init.method ?? "GET", headers: new Headers(init.headers), body: init.body ? JSON.parse(String(init.body)) : undefined };
      calls.push(call);
      return override?.(call) ?? fake(call);
    }),
  );
  setAgent(specOf(RESOURCE_WM));
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
  media?.restore();
  media = undefined;
  restoreWidth();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  document.documentElement.classList.remove("dark");
});

const writes = (method: string) => calls.filter((call) => call.method === method);
const click = (id: string) => fireEvent.click(screen.getByTestId(id));

// ------------------------------------------------------------------ the If-Match flow, end to end

describe("working memory: create, edit, conflict, clear (If-Match end to end)", () => {
  const edit = (text: string) => fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: text } });
  const putMatches = () => writes("PUT").map((call) => call.headers.get("If-Match"));

  it("creates with version 0, edits with the current version, handles a 409 with Reload, then clears", async () => {
    render(<WorkingMemoryEditor target={target} format="markdown" starter={"# User\n- Name:"} />);
    // 1. No note: Create starts from the starter and sends If-Match "0" (create only).
    fireEvent.click(await screen.findByTestId("memory-working-create"));
    edit("# User\n- Name: Ada");
    click("memory-working-save");
    await waitFor(() => expect(screen.getByTestId("memory-working-content")).toHaveTextContent("Name: Ada"));
    expect(screen.getByTestId("memory-working-content").parentElement).toHaveTextContent("Version 1");
    // 2. Edit: the PUT carries the version the edit started from.
    click("memory-working-edit");
    edit("# User\n- Name: Ada Lovelace");
    click("memory-working-save");
    await waitFor(() => expect(screen.getByTestId("memory-working-content")).toHaveTextContent("Ada Lovelace"));
    expect(putMatches()).toEqual(['"0"', '"1"']);
    // 3. Someone else writes version 3 while this edit (from version 2) is open: 409, Reload.
    click("memory-working-edit");
    edit("my concurrent edit");
    notes.set(NOTE_PATH, { content: "their text", version: 3 });
    click("memory-working-save");
    const conflictBox = await screen.findByTestId("memory-working-conflict");
    expect(conflictBox).toHaveTextContent("working memory is at version 3, not the If-Match version 2");
    expect(conflictBox).toHaveTextContent("Reload replaces it");
    expect(notes.get(NOTE_PATH)).toEqual({ content: "their text", version: 3 });
    expect(screen.getByTestId("memory-working-save")).toBeDisabled();
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue("my concurrent edit");
    click("memory-working-reload");
    await waitFor(() => expect(screen.getByTestId("memory-working-textarea")).toHaveValue("their text"));
    expect(screen.queryByTestId("memory-working-conflict")).toBeNull();
    expect(screen.getByTestId("memory-working-save")).toBeEnabled();
    // 4. Applied again on the reloaded version: If-Match "3", stored as version 4.
    edit("their text, plus mine");
    click("memory-working-save");
    await waitFor(() => expect(screen.getByTestId("memory-working-content")).toHaveTextContent("plus mine"));
    expect(putMatches()).toEqual(['"0"', '"1"', '"2"', '"3"']);
    expect(notes.get(NOTE_PATH)?.version).toBe(4);
    // 5. Clear confirms, then deletes with the current version.
    click("memory-working-clear");
    const dialog = await screen.findByRole("dialog", { name: "Clear working memory?" });
    expect(writes("DELETE")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Clear" }));
    await screen.findByTestId("memory-working-empty");
    expect(writes("DELETE").map((call) => call.headers.get("If-Match"))).toEqual(['"4"']);
    expect(notes.has(NOTE_PATH)).toBe(false);
  });

  it("a create that lost the race (the note appeared meanwhile) is refused with 409 and Reload shows the note", async () => {
    render(<WorkingMemoryEditor target={target} format="markdown" starter="# User" />);
    fireEvent.click(await screen.findByTestId("memory-working-create"));
    notes.set(NOTE_PATH, { content: "already there", version: 1 });
    click("memory-working-save");
    expect(await screen.findByTestId("memory-working-conflict")).toHaveTextContent("working memory is at version 1, not the If-Match version 0");
    click("memory-working-reload");
    await waitFor(() => expect(screen.getByTestId("memory-working-textarea")).toHaveValue("already there"));
  });

  it("Cancel leaves the stored note alone and sends nothing", async () => {
    notes.set(NOTE_PATH, { content: "keep", version: 2 });
    render(<WorkingMemoryEditor target={target} format="markdown" starter="" />);
    fireEvent.click(await screen.findByTestId("memory-working-edit"));
    edit("changed");
    click("memory-working-cancel");
    expect(screen.getByTestId("memory-working-content")).toHaveTextContent("keep");
    expect(writes("PUT")).toHaveLength(0);
  });

  it("a note that changes while it is being edited is flagged stale; saving is still refused by the API", async () => {
    notes.set(NOTE_PATH, { content: "v2 text", version: 2 });
    const view = render(<WorkingMemoryEditor target={target} format="markdown" starter="" refreshKey={0} />);
    fireEvent.click(await screen.findByTestId("memory-working-edit"));
    edit("mine");
    notes.set(NOTE_PATH, { content: "agent wrote", version: 3 });
    view.rerender(<WorkingMemoryEditor target={target} format="markdown" starter="" refreshKey={1} />);
    expect(await screen.findByTestId("memory-working-stale")).toHaveTextContent("version 3");
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue("mine");
    click("memory-working-save");
    expect(await screen.findByTestId("memory-working-conflict")).toHaveTextContent("not the If-Match version 2");
  });

  it("a 409 on Clear (the note changed) keeps it, says why and reloads the newer note", async () => {
    notes.set(NOTE_PATH, { content: "old", version: 2 });
    render(<WorkingMemoryEditor target={target} format="markdown" starter="" />);
    fireEvent.click(await screen.findByTestId("memory-working-clear"));
    notes.set(NOTE_PATH, { content: "newer", version: 5 });
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear" }));
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("working memory is at version 5, not the If-Match version 2");
    expect(notes.has(NOTE_PATH)).toBe(true);
    await waitFor(() => expect(writes("GET").filter((call) => call.path === NOTE_PATH).length).toBeGreaterThan(1));
  });
});

describe("working memory: schema (JSON) and template (Markdown) modes", () => {
  it("schema mode sends an object, shows indented JSON, and a 422 from the API is shown as the save error", async () => {
    notes.set(NOTE_PATH, { content: { name: "Ada", tags: ["a"] }, version: 1 });
    render(<WorkingMemoryEditor target={target} format="json" starter="{}" />);
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent('"name": "Ada"');
    click("memory-working-edit");
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue(JSON.stringify({ name: "Ada", tags: ["a"] }, null, 2));
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: '{"name": "Grace"}' } });
    click("memory-working-save");
    await waitFor(() => expect(screen.getByTestId("memory-working-content")).toHaveTextContent("Grace"));
    expect(writes("PUT")[0].body).toEqual({ content: { name: "Grace" } });
    expect(notes.get(NOTE_PATH)).toEqual({ content: { name: "Grace" }, version: 2 });
    // The schema refuses it: the API's path-only message, as a (non-409) save error; the edit stays.
    override = (call) => (call.method === "PUT" ? json({ detail: [{ msg: "name: expected string" }, { msg: "tags: expected array" }] }, 422) : undefined);
    click("memory-working-edit");
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: '{"name": 1}' } });
    click("memory-working-save");
    expect(await screen.findByTestId("memory-working-save-error")).toHaveTextContent("name: expected string, tags: expected array");
    expect(screen.queryByTestId("memory-working-conflict")).toBeNull();
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue('{"name": 1}');
  });

  it("schema mode refuses invalid JSON, null, arrays and scalars before any request; Create starts from {}", async () => {
    render(<WorkingMemoryEditor target={target} format="json" starter="{}" />);
    fireEvent.click(await screen.findByTestId("memory-working-create"));
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue("{}");
    for (const [text, message] of [["{nope", "Not valid JSON"], ["null", "JSON object"], ["[]", "JSON object"], ['"text"', "JSON object"], ["3", "JSON object"]]) {
      fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: text } });
      click("memory-working-save");
      expect(await screen.findByTestId("memory-working-save-error")).toHaveTextContent(message);
    }
    expect(writes("PUT")).toHaveLength(0);
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: '{"a": 1}' } });
    expect(screen.queryByTestId("memory-working-save-error")).toBeNull();
    click("memory-working-save");
    await screen.findByTestId("memory-working-content");
    expect(writes("PUT")[0].headers.get("If-Match")).toBe('"0"');
  });

  it("template mode sends the text as a string, whatever it looks like", async () => {
    render(<WorkingMemoryEditor target={target} format="markdown" starter="" />);
    fireEvent.click(await screen.findByTestId("memory-working-create"));
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: '{"looks": "like json"}' } });
    click("memory-working-save");
    await screen.findByTestId("memory-working-content");
    expect(writes("PUT")[0].body).toEqual({ content: '{"looks": "like json"}' });
  });

  it("parseNoteText: Markdown as is; JSON only as an object", () => {
    expect(parseNoteText("# x", "markdown")).toEqual({ content: "# x" });
    expect(parseNoteText('{"a":1}', "json")).toEqual({ content: { a: 1 } });
    for (const bad of ["", "{", "[]", "1", "null", "true"]) expect(parseNoteText(bad, "json")).toHaveProperty("error");
  });

  it("an unreachable note shows the error with Retry, and a 404 is simply 'no note'", async () => {
    override = (call) => (call.method === "GET" ? json({ detail: "boom" }, 500) : undefined);
    render(<WorkingMemoryEditor target={target} format="markdown" starter="" />);
    expect(await screen.findByTestId("memory-working-error")).toHaveTextContent("boom");
    expect(screen.queryByTestId("memory-working-create")).toBeNull();
    override = null;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("memory-working-empty")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------ observations

describe("observations: expired markers and delete-newest, as the API has them", () => {
  const rowsOf = () => screen.getAllByTestId("memory-observation-row");
  const confirmDialog = async (name: string) => fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name }));

  // [description, rows, active ids oldest first]. Every row the API would delete is the only row
  // with Delete; every row that sits before a newer marker says why.
  const scenarios: [string, Observation[]][] = [
    ["one plain row", [row("o1", 1, 4)]],
    ["a plain newest", [row("o1", 1, 4), row("o2", 5, 8)]],
    ["a marker newest", [row("o1", 1, 4), marker("m1", 5, 8)]],
    ["a marker in the middle", [row("o1", 1, 4), marker("m1", 5, 8), row("o2", 9, 12)]],
    ["two markers", [marker("m1", 1, 4), marker("m2", 5, 8)]],
    // As the store writes it: the reflection supersedes the rows it replaces, the marker included.
    ["a marker then a reflection", [row("o1", 1, 3, { superseded_by: "r1" }), marker("m1", 4, 6, { superseded_by: "r1" }), row("r1", 1, 9, { kind: "reflection" })]],
    ["a superseded row is not listed", [row("o0", 1, 2, { superseded_by: "r1" }), row("o1", 3, 6), row("r1", 1, 2, { kind: "reflection" })]],
  ];

  it.each(scenarios)("%s: Delete is offered exactly where the API accepts it", async (_name, rows) => {
    observations.c1 = rows;
    render(<ObservationsList conversationId="c1" />);
    await screen.findAllByTestId("memory-observation-row");
    const active = rows.filter((item) => !item.superseded_by);
    expect(rowsOf()).toHaveLength(active.length);
    const sorted = [...active].sort((a, b) => a.covers_to_turn - b.covers_to_turn);
    const newest = newestObservation(rows)!;
    for (const [index, item] of sorted.entries()) {
      const element = rowsOf()[index];
      expect(within(element).queryByTestId("memory-observation-delete") !== null).toBe(apiAcceptsDelete(rows, item.id));
      expect(element).toHaveAttribute("data-expired", item.expired ? "true" : "false");
      // The explanation is for rows before a newer marker (where the API's 409 is about expiry).
      const behindMarker = newest.expired === true && newest.id !== item.id && newest.covers_to_turn > item.covers_to_turn;
      expect(within(element).queryByTestId("memory-observation-blocked") !== null).toBe(behindMarker);
      expect(blockedByExpired(rows, item)).toBe(behindMarker);
    }
    expect(screen.getAllByTestId("memory-observation-delete")).toHaveLength(1);
  });

  it("a marker shows Expired and an explanation, never its empty text, and says 'expired' in its Delete name", async () => {
    observations.c1 = [row("o1", 1, 4), marker("m1", 5, 8)];
    render(<ObservationsList conversationId="c1" />);
    const [plain, expired] = await screen.findAllByTestId("memory-observation-row");
    expect(within(plain).queryByTestId("memory-observation-expired")).toBeNull();
    expect(plain).toHaveTextContent("Notes o1");
    expect(plain).toHaveTextContent("40 tokens");
    expect(within(expired).getByTestId("memory-observation-expired")).toHaveTextContent("Expired");
    expect(expired).toHaveTextContent("Turns 5–8");
    expect(expired).toHaveTextContent("text aged out");
    expect(expired).not.toHaveTextContent("tokens");
    expect(within(expired).getByRole("button", { name: "Delete expired observation (turns 5–8)" })).toBeInTheDocument();
    expect(within(plain).getByTestId("memory-observation-blocked")).toHaveTextContent("delete it first (or delete all)");
  });

  it("covered_to_turn from the API is what the note says, markers included", async () => {
    observations.c1 = [row("o1", 1, 4), marker("m1", 5, 11)];
    render(<ObservationsList conversationId="c1" />);
    expect(await screen.findByTestId("memory-observations-covered")).toHaveTextContent("Turns 1–11 reach the model as these notes; later turns are sent verbatim.");
    expect(screen.getByRole("heading", { name: /Observations/ })).toHaveTextContent("(2)");
    // Nothing covered: no line.
    cleanup();
    observations.c1 = [];
    render(<ObservationsList conversationId="c1" />);
    await screen.findByTestId("memory-observations-empty");
    expect(screen.queryByTestId("memory-observations-covered")).toBeNull();
    expect(screen.queryByTestId("memory-observations-delete-all")).toBeNull();
  });

  it("deleting a marker confirms with its own words, deletes it, and the row before it becomes deletable", async () => {
    observations.c1 = [row("o1", 1, 4), marker("m1", 5, 8)];
    render(<ObservationsList conversationId="c1" />);
    await screen.findAllByTestId("memory-observation-row");
    click("memory-observation-delete");
    const dialog = await screen.findByRole("dialog", { name: "Delete the expired observation?" });
    expect(dialog).toHaveTextContent("turns 5–8");
    expect(writes("DELETE")).toHaveLength(0);
    await confirmDialog("Delete");
    await waitFor(() => expect(rowsOf()).toHaveLength(1));
    expect(writes("DELETE")[0].path).toBe("/api/v1/conversations/c1/memory/observations/m1");
    expect(within(rowsOf()[0]).getByTestId("memory-observation-delete")).toBeInTheDocument();
    expect(screen.queryByTestId("memory-observation-blocked")).toBeNull();
    expect(screen.getByTestId("memory-observations-covered")).toHaveTextContent("Turns 1–4");
  });

  it("a 409 (the observer added a newer row meanwhile) stays in the dialog with the API's detail and reloads the list", async () => {
    observations.c1 = [row("o1", 1, 4), row("o2", 5, 8)];
    render(<ObservationsList conversationId="c1" />);
    await screen.findAllByTestId("memory-observation-row");
    // The observer inserts o3 after the list was read: o2 is no longer the newest.
    observations.c1 = [...observations.c1, row("o3", 9, 12)];
    click("memory-observation-delete");
    await confirmDialog("Delete");
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("only the newest active observation (expired ones included) can be deleted");
    expect(observations.c1.map((item) => item.id)).toEqual(["o1", "o2", "o3"]);
    // The reload shows o3 as the one with Delete; the dialog is still open until cancelled.
    await waitFor(() => expect(rowsOf()).toHaveLength(3));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(within(rowsOf()[2]).getByTestId("memory-observation-delete")).toBeInTheDocument();
    expect(within(rowsOf()[1]).queryByTestId("memory-observation-delete")).toBeNull();
  });

  it("a 409 whose newest row is a marker names expired ones, and the fake API agrees the older row is refused", async () => {
    observations.c1 = [row("o1", 1, 4), row("o2", 5, 8)];
    render(<ObservationsList conversationId="c1" />);
    await screen.findAllByTestId("memory-observation-row");
    observations.c1 = [...observations.c1, marker("m1", 9, 12)];
    click("memory-observation-delete");
    await confirmDialog("Delete");
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("expired ones included");
    await waitFor(() => expect(rowsOf()).toHaveLength(3));
    expect(within(rowsOf()[1]).getByTestId("memory-observation-blocked")).toBeInTheDocument();
  });

  it("a 404 (already deleted elsewhere) is shown as the API's message and the list reloads", async () => {
    observations.c1 = [row("o1", 1, 4)];
    render(<ObservationsList conversationId="c1" />);
    await screen.findAllByTestId("memory-observation-row");
    observations.c1 = [];
    click("memory-observation-delete");
    await confirmDialog("Delete");
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("Observation not found");
    await screen.findByTestId("memory-observations-empty");
  });

  it("Delete all confirms, deletes everything (markers too) and shows the count", async () => {
    observations.c1 = [row("o1", 1, 4), marker("m1", 5, 8), row("o2", 9, 12)];
    render(<ObservationsList conversationId="c1" />);
    await screen.findAllByTestId("memory-observation-row");
    click("memory-observations-delete-all");
    const dialog = await screen.findByRole("dialog", { name: "Delete all observations?" });
    expect(dialog).toHaveTextContent("Every observation and reflection of this conversation is deleted");
    expect(writes("DELETE")).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete all" }));
    await screen.findByTestId("memory-observations-empty");
    expect(writes("DELETE").map((call) => `${call.method} ${call.path}`)).toEqual(["DELETE /api/v1/conversations/c1/memory/observations"]);
    expect(observations.c1).toEqual([]);
  });

  it("Delete all is also the way out when an expired marker blocks the older rows", async () => {
    observations.c1 = [row("o1", 1, 4), marker("m1", 5, 8)];
    render(<ObservationsList conversationId="c1" />);
    await screen.findAllByTestId("memory-observation-row");
    expect(within(rowsOf()[0]).queryByTestId("memory-observation-delete")).toBeNull();
    expect(screen.getByTestId("memory-observations-delete-all")).toBeEnabled();
  });

  it("reloads when the refresh key changes (a memory write was seen) and for another conversation", async () => {
    observations.c1 = [row("o1", 1, 4)];
    observations.c2 = [row("p1", 1, 2), row("p2", 3, 5)];
    const view = render(<ObservationsList conversationId="c1" refreshKey={0} />);
    await screen.findAllByTestId("memory-observation-row");
    observations.c1 = [row("o1", 1, 4), row("o2", 5, 9)];
    view.rerender(<ObservationsList conversationId="c1" refreshKey={1} />);
    await waitFor(() => expect(rowsOf()).toHaveLength(2));
    view.rerender(<ObservationsList conversationId="c2" refreshKey={1} />);
    await waitFor(() => expect(rowsOf()[0]).toHaveTextContent("Notes p1"));
    expect(calls.map((call) => call.path)).toContain("/api/v1/conversations/c2/memory/observations");
  });
});

describe("lib/memory pure rules", () => {
  it("newestObservation ignores superseded rows and counts markers; blockedByExpired only for a newer marker", () => {
    expect(newestObservation([])).toBeNull();
    const rows = [row("a", 1, 3), row("b", 4, 6, { superseded_by: "z" }), marker("m", 7, 9), row("z", 1, 6, { kind: "reflection" })];
    expect(newestObservation(rows)?.id).toBe("m");
    expect(blockedByExpired(rows, rows[0])).toBe(true);
    expect(blockedByExpired(rows, rows[2])).toBe(false);
    expect(blockedByExpired([row("a", 1, 3), row("c", 4, 6)], row("a", 1, 3))).toBe(false);
    expect(blockedByExpired([], row("a", 1, 3))).toBe(false);
  });

  it("recallSummary reads the recall embedding event only, counts only, and a failure carries its code", () => {
    expect(recallSummary([])).toEqual({ status: "none" });
    expect(recallSummary([{ seq: 1, type: "embedding", data: { stage: "index", hits: 9 } }])).toEqual({ status: "none" });
    expect(recallSummary([{ seq: 1, type: "embedding", data: { stage: "recall", hits: 3, turns: 2, groups: 1, messages: 4, text: "secret" } }])).toEqual({ status: "done", hits: 3, turns: 2, groups: 1, messages: 4 });
    expect(recallSummary([{ seq: 1, type: "embedding", data: { stage: "recall" } }])).toEqual({ status: "done", hits: 0, turns: 0, groups: 0, messages: 0 });
    expect(recallSummary([{ seq: 1, type: "embedding", data: { stage: "recall", status: "failed", error_code: "embedding_unavailable" } }])).toEqual({ status: "failed", errorCode: "embedding_unavailable" });
    expect(recallSummary([{ seq: 1, type: "embedding", data: { stage: "recall", status: "failed" } }])).toEqual({ status: "failed", errorCode: null });
    expect(recallSummary([{ seq: 1, type: "embedding", data: { stage: "recall", hits: "3", turns: Number.NaN } }])).toEqual({ status: "done", hits: 0, turns: 0, groups: 0, messages: 0 });
  });

  it("postTurnReported: observer events when observational is on, else the index event, else always", () => {
    const observe = { seq: 5, type: "memory_updated", data: { stage: "observe" } };
    const reflect = { seq: 6, type: "memory_updated", data: { stage: "reflect" } };
    const failed = { seq: 7, type: "memory_observe_failed", data: {} };
    const index = { seq: 8, type: "embedding", data: { stage: "index" } };
    const recall = { seq: 2, type: "embedding", data: { stage: "recall" } };
    const working = { seq: 3, type: "memory_updated", data: { stage: "working" } };
    const both = { observational: true, semanticRecall: true };
    expect(postTurnReported([], both)).toBe(false);
    expect(postTurnReported([recall, working, index], both)).toBe(false);
    for (const event of [observe, reflect, failed]) expect(postTurnReported([event], both)).toBe(true);
    expect(postTurnReported([index], { observational: false, semanticRecall: true })).toBe(true);
    expect(postTurnReported([recall, observe], { observational: false, semanticRecall: true })).toBe(false);
    expect(postTurnReported([], { observational: false, semanticRecall: false })).toBe(true);
    expect(memoryWrites([observe, working, failed, index, recall])).toBe(3);
    expect(POST_TURN_POLL_MS).toBe(1500);
    expect(POST_TURN_TIMEOUT_MS).toBe(20_000);
    expect(errorStatus(Object.assign(new Error("x"), { status: 409 }))).toBe(409);
    expect(errorStatus(new Error("x"))).toBeNull();
    expect(errorStatus(null)).toBeNull();
  });
});

// --------------------------------------------------------------------- the "updating" poll

describe("Memory updating: the post-turn poll (real intervals)", () => {
  const turn = (status: string, events: LatestTurn["events"] = [], runId = "r1"): LatestTurn => ({ runId, status, events });
  const COMPLETED = [{ seq: 4, type: "run_completed", data: {} }];
  const observe = { seq: 5, type: "memory_updated", data: { stage: "observe", notes: 2 } };
  const OBSERVER = { observational: true, semanticRecall: false };
  const RECALL = { observational: false, semanticRecall: true };
  const BOTH = { observational: true, semanticRecall: true };
  // (run id, after seq); every poll read also passes the poll's AbortSignal (checked below).
  const pollCalls = () => vi.mocked(listEvents).mock.calls.map((args) => args.slice(0, 2));
  const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  /** Renders the hook with a running turn, then completes it: a turn this page watched. */
  function watched(features: { observational: boolean; semanticRecall: boolean }, finalStatus = "completed") {
    const hook = renderHook(({ t }) => useMemoryActivity(t, features), { initialProps: { t: turn("running") } });
    hook.rerender({ t: turn(finalStatus, COMPLETED) });
    return hook;
  }
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("starts for a watched turn: first read after 1.5 s, from the last seen seq", async () => {
    const { result } = watched(OBSERVER);
    expect(result.current.updating).toBe(true);
    await advance(POST_TURN_POLL_MS - 1);
    expect(pollCalls()).toHaveLength(0);
    await advance(1);
    expect(pollCalls()).toEqual([["r1", 4]]);
    expect(vi.mocked(listEvents).mock.calls[0][2]).toBeInstanceOf(AbortSignal);
    // Still waiting: another read 1.5 s later, from the same seq (nothing new came).
    await advance(POST_TURN_POLL_MS);
    expect(pollCalls()).toEqual([["r1", 4], ["r1", 4]]);
    expect(result.current.updating).toBe(true);
  });

  it("never starts for a conversation that was opened with a finished turn, nor when the turn was never seen running", async () => {
    const opened = renderHook(() => useMemoryActivity(turn("completed", COMPLETED), BOTH));
    expect(opened.result.current.updating).toBe(false);
    // Opening another old conversation (another run id, already completed) is not a watched finish either.
    const switched = renderHook(({ t }) => useMemoryActivity(t, BOTH), { initialProps: { t: turn("completed", COMPLETED, "r1") } });
    switched.rerender({ t: turn("completed", COMPLETED, "r2") });
    expect(switched.result.current.updating).toBe(false);
    await advance(POST_TURN_TIMEOUT_MS * 2);
    expect(pollCalls()).toHaveLength(0);
  });

  it("starts only when the spec has post-turn steps and the turn completed", async () => {
    const plain = watched({ observational: false, semanticRecall: false });
    expect(plain.result.current.updating).toBe(false);
    for (const status of ["failed", "needs_attention"]) {
      const bad = watched(BOTH, status);
      expect(bad.result.current.updating).toBe(false);
    }
    // A turn that was queued then completed was watched too (not terminal when seen).
    const queued = renderHook(({ t }) => useMemoryActivity(t, OBSERVER), { initialProps: { t: turn("queued") } });
    queued.rerender({ t: turn("completed", COMPLETED) });
    expect(queued.result.current.updating).toBe(true);
    queued.unmount();
    await advance(POST_TURN_TIMEOUT_MS * 2);
    expect(pollCalls()).toHaveLength(0);
  });

  it("stops when the observer reports (observe, reflect or failed), and bumps the revision", async () => {
    for (const [index, event] of [observe, { seq: 5, type: "memory_updated", data: { stage: "reflect" } }, { seq: 5, type: "memory_observe_failed", data: {} }].entries()) {
      vi.mocked(listEvents).mockClear();
      vi.mocked(listEvents).mockResolvedValue([event] as never);
      const { result } = watched(OBSERVER);
      await advance(POST_TURN_POLL_MS);
      expect(result.current.updating, `event ${index}`).toBe(false);
      expect(result.current.revision).toBe(1);
      await advance(POST_TURN_TIMEOUT_MS);
      expect(pollCalls()).toHaveLength(1);
    }
  });

  it("with observational memory on, an index event alone does not stop it; a working-memory write bumps the revision", async () => {
    const working = { seq: 5, type: "memory_updated", data: { stage: "working" } };
    vi.mocked(listEvents).mockResolvedValueOnce([working, { seq: 6, type: "embedding", data: { stage: "index" } }] as never).mockResolvedValue([] as never);
    const { result } = watched(BOTH);
    await advance(POST_TURN_POLL_MS);
    expect(result.current.revision).toBe(1);
    expect(result.current.updating).toBe(true);
    await advance(POST_TURN_POLL_MS);
    expect(pollCalls()[1]).toEqual(["r1", 6]);
    expect(result.current.updating).toBe(true);
  });

  it("recall-only: stops on the index event; a recall event does not stop it and fills the summary", async () => {
    vi.mocked(listEvents)
      .mockResolvedValueOnce([{ seq: 5, type: "embedding", data: { stage: "recall", hits: 1, turns: 1, groups: 1, messages: 2 } }] as never)
      .mockResolvedValueOnce([{ seq: 6, type: "embedding", data: { stage: "index" } }] as never);
    const { result } = watched(RECALL);
    await advance(POST_TURN_POLL_MS);
    expect(result.current.updating).toBe(true);
    expect(result.current.recall).toEqual({ status: "done", hits: 1, turns: 1, groups: 1, messages: 2 });
    await advance(POST_TURN_POLL_MS);
    expect(result.current.updating).toBe(false);
    await advance(POST_TURN_TIMEOUT_MS);
    expect(pollCalls()).toHaveLength(2);
  });

  it("gives up after 20 s (within one poll interval) when nothing reports, and then reads nothing more", async () => {
    const { result } = watched(OBSERVER);
    await advance(POST_TURN_TIMEOUT_MS - 1000);
    expect(result.current.updating).toBe(true);
    expect(pollCalls().length).toBeGreaterThanOrEqual(12);
    await advance(1000 + POST_TURN_POLL_MS);
    expect(result.current.updating).toBe(false);
    const total = pollCalls().length;
    expect(total).toBeLessThanOrEqual(Math.ceil(POST_TURN_TIMEOUT_MS / POST_TURN_POLL_MS) + 1);
    await advance(POST_TURN_TIMEOUT_MS * 2);
    expect(pollCalls()).toHaveLength(total);
  });

  it("keeps trying after a failed read, until the deadline", async () => {
    vi.mocked(listEvents).mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce([observe] as never);
    const { result } = watched(OBSERVER);
    await advance(POST_TURN_POLL_MS);
    expect(result.current.updating).toBe(true);
    await advance(POST_TURN_POLL_MS);
    expect(result.current.updating).toBe(false);
    expect(pollCalls()).toHaveLength(2);
  });

  it("is cleaned up on unmount: no timers fire, nothing is read, even with a read in flight", async () => {
    let release: (events: unknown[]) => void = () => undefined;
    vi.mocked(listEvents).mockImplementationOnce(() => new Promise((resolve) => { release = resolve as never; }));
    const first = watched(OBSERVER);
    await advance(POST_TURN_POLL_MS);
    expect(pollCalls()).toHaveLength(1);
    first.unmount();
    await act(async () => { release([observe]); });
    await advance(POST_TURN_TIMEOUT_MS * 2);
    expect(pollCalls()).toHaveLength(1);
    // Unmounted while waiting for the first tick: the timer is cleared too.
    vi.mocked(listEvents).mockClear();
    const second = watched(OBSERVER);
    second.unmount();
    await advance(POST_TURN_TIMEOUT_MS);
    expect(pollCalls()).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a newer turn drops the previous turn's poll and what it read", async () => {
    vi.mocked(listEvents).mockResolvedValueOnce([{ seq: 5, type: "embedding", data: { stage: "recall", hits: 2, turns: 1, groups: 1, messages: 2 } }] as never);
    const hook = watched(OBSERVER);
    await advance(POST_TURN_POLL_MS);
    expect(hook.result.current.recall.status).toBe("done");
    hook.rerender({ t: turn("running", [], "r2") });
    expect(hook.result.current.updating).toBe(false);
    expect(hook.result.current.recall).toEqual({ status: "none" });
    vi.mocked(listEvents).mockClear();
    await advance(POST_TURN_TIMEOUT_MS);
    expect(pollCalls().filter(([runId]) => runId === "r1")).toHaveLength(0);
  });
});

// ------------------------------------------------------------- the poll and card, in the chat

const ev = (seq: number, type: string, data: object = {}) => ({ seq, type, data, ts: T0 }) as never;
const answer = (seq: number) => ev(seq, "model_message", { model_request: 0, text: "Hi there", is_final: true });
const box = () => screen.getByRole("textbox", { name: "Message" });
async function send(text: string) {
  fireEvent.change(box(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(subscribeRun).toHaveBeenCalled());
}
const emit = (...events: unknown[]) => {
  const handlers = vi.mocked(subscribeRun).mock.calls[0][1];
  act(() => events.forEach((event) => handlers.onEvent?.(event as never)));
};
const credentialScopeOf = () => credentialScope();
// Poll reads pass (run id, after seq, signal); loading a conversation reads with the run id only.
const pollReads = () => vi.mocked(listEvents).mock.calls.filter((args) => args.length >= 2);

describe("in the chat: the card follows a watched turn, never an opened one", () => {
  beforeEach(() => {
    setAgent(specOf(FULL));
    window.localStorage.setItem(MEMORY_CARD_KEY, "open");
    observations.c1 = [row("o1", 1, 4)];
    // The API's ids are its own: the fake keeps this conversation's notes under "c1".
    vi.mocked(createConversation).mockImplementation(async (agentId, title, _id, resourceId) => ({ id: "c1", agent_id: agentId, title, resource_id: resourceId ?? "dev-user", turn_count: 0, created_at: "", updated_at: "" }) as never);
  });

  it("a watched turn: recall counts from its events, 'Memory updating' until the observer reports, then the notes reload", async () => {
    render(<ChatTab agentId="a1" />);
    await screen.findByTestId("memory-inspector-empty");
    await send("Hello");
    await screen.findByTestId("memory-observations");
    emit(ev(0, "run_started", { input: "Hello" }), ev(1, "embedding", { stage: "recall", hits: 4, turns: 2, groups: 1, messages: 3 }), answer(2), ev(3, "run_completed"));
    expect(await screen.findByTestId("memory-updating")).toHaveTextContent("Memory updating");
    expect(screen.getByTestId("memory-recall-summary")).toHaveAttribute("data-status", "done");
    expect(screen.getByTestId("memory-recall-summary")).toHaveTextContent("recalled 2 earlier turns (4 hits); 3 messages in 1 group reached the model.");
    expect(pollReads()).toHaveLength(0);
    // The observer writes after run_completed: the poll finds it, the list reloads with the new row.
    observations.c1 = [row("o1", 1, 4), row("o2", 5, 9)];
    vi.mocked(listEvents).mockResolvedValue([ev(4, "memory_updated", { stage: "observe", notes: 1 })]);
    await waitFor(() => expect(screen.queryByTestId("memory-updating")).toBeNull(), { timeout: 4000 });
    expect(pollReads()[0]?.[1]).toBe(3);
    await waitFor(() => expect(screen.getAllByTestId("memory-observation-row")).toHaveLength(2));
    expect(pollReads().length).toBe(1);
  }, 10_000);

  it("collapsed, the card says Updating… in its header", async () => {
    window.localStorage.setItem(MEMORY_CARD_KEY, "closed");
    render(<ChatTab agentId="a1" />);
    await screen.findByTestId("memory-card-toggle");
    await send("Hello");
    emit(ev(0, "run_started", { input: "Hello" }), answer(1), ev(2, "run_completed"));
    expect(await screen.findByTestId("memory-card-updating")).toHaveTextContent("Updating");
    expect(screen.queryByTestId("memory-updating")).toBeNull();
  });

  it("an opened old conversation never polls: no 'updating', no event reads after the load", async () => {
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c1", "dev-user")], next_before: null } as never);
    vi.mocked(getConversation).mockResolvedValue({
      ...conversation("c1", "dev-user"),
      runs: [{ run_id: "r9", agent_id: "a1", version: 1, status: "completed", display_status: "completed", input: "q", output: "a", error: null, heartbeat_at: null, created_at: "", updated_at: "", trace_id: null, conversation_id: "c1", turn: 1 }],
    } as never);
    vi.mocked(listEvents).mockResolvedValue([ev(0, "run_started", { input: "Hello" }), answer(1), ev(2, "run_completed")]);
    render(<ChatTab agentId="a1" conversationId="c1" />);
    await screen.findByTestId("memory-observations");
    await waitFor(() => expect(listEvents).toHaveBeenCalledWith("r9"));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, POST_TURN_POLL_MS + 400)); });
    expect(screen.queryByTestId("memory-updating")).toBeNull();
    expect(pollReads()).toHaveLength(0);
  }, 10_000);

  it("leaving the page mid-poll stops it", async () => {
    const view = render(<ChatTab agentId="a1" />);
    await screen.findByTestId("memory-inspector-empty");
    await send("Hello");
    emit(ev(0, "run_started", { input: "Hello" }), answer(1), ev(2, "run_completed"));
    await screen.findByTestId("memory-updating");
    view.unmount();
    await new Promise((resolve) => setTimeout(resolve, POST_TURN_POLL_MS + 400));
    expect(pollReads()).toHaveLength(0);
  }, 10_000);
});

// ----------------------------------------------------------------- the resource id charset

// Mirrors eap_run_store.validate_resource_id (RESOURCE_ID_PATTERN and the dot-only rule). The table
// is read by the cross-check script in the scratchpad (rm18-resource-ids.py), which runs the Python
// validator over every line: keep one JSON entry per line, [id, valid].
// ID-TABLE-START
const ID_TABLE: [string, boolean][] = [
  ["alice", true],
  ["Alice_01", true],
  ["alice@example.com", true],
  ["user:42", true],
  ["a-b.c_d:e@f", true],
  ["0", true],
  ["a", true],
  ["...a", true],
  [".a.", true],
  ["a..", true],
  ["-", true],
  [":", true],
  ["@", true],
  ["_", true],
  [".", false],
  ["..", false],
  ["...", false],
  ["", false],
  [" ", false],
  [" alice", false],
  ["alice ", false],
  ["al ice", false],
  ["alice\n", false],
  ["\nalice", false],
  ["al\tice", false],
  ["alice\u0000", false],
  ["alice/bob", false],
  ["alice\\bob", false],
  ["../etc", false],
  ["a%2Fb", false],
  ["a?b", false],
  ["a#b", false],
  ["a b@c.d", false],
  ["alice+tag@example.com", false],
  ["alice,bob", false],
  ["alice;bob", false],
  ["a=b", false],
  ["a*", false],
  ["(a)", false],
  ["\u00e9", false],
  ["caf\u00e9", false],
  ["\u0663", false],
  ["\uff21", false],
  ["\u4e2d\u6587", false],
  ["\ud83d\ude00", false],
  ["a\u200bb", false],
  ["a\u2028b", false],
  ["a\u00a0b", false],
];
// ID-TABLE-END
const LONG_OK = "a".repeat(128);
const LONG_BAD = "a".repeat(129);

describe("resource id validation mirrors the Python validator (table in this file)", () => {
  it.each([...ID_TABLE, [LONG_OK, true], [LONG_BAD, false]] as [string, boolean][])("resourceIdProblem(%j) is %s", (id, valid) => {
    // An empty id is "not set" to callers, so it is not a problem here; every other row is as the table says.
    if (id === "") return expect(resourceIdProblem(id)).not.toBeNull();
    expect(resourceIdProblem(id) === null).toBe(valid);
  });

  it("the two messages are the server's two reasons", () => {
    expect(resourceIdProblem("a b")).toMatch(/1 to 128 characters of A-Z, a-z, 0-9, '_', '.', ':', '@' or '-'/);
    expect(resourceIdProblem("..")).toBe("A user id must not consist of dots only.");
  });
});

// ------------------------------------------------------------------------------ the Memory tab

describe("Memory tab: visibility", () => {
  const tabs = () => within(screen.getByRole("navigation", { name: "Agent sections" }));
  function openLayout(path: string) {
    nav.path = path;
    media = mockMatchMedia(true);
    return render(
      <FrameHarness>
        <AgentLayout agentId="a1">
          <div />
        </AgentLayout>
      </FrameHarness>,
    );
  }

  it("an agent without memory has no Memory tab", async () => {
    setAgent(specOf());
    openLayout("/agents/a1/chat");
    await screen.findByRole("heading", { level: 1, name: "Support bot" });
    await waitFor(() => expect(api.versions.list).toHaveBeenCalled());
    expect(tabs().getAllByRole("link").map((link) => link.textContent)).toEqual(["Chat", "Traces", "Editor"]);
  });

  it.each([
    ["the draft has memory (never published)", specOf({ working: {} }), null],
    ["the draft has memory, the latest version does not", specOf({ working: {} }), specOf()],
    ["only the latest version has memory", specOf(), specOf({ working: {} })],
  ])("shown when %s", async (_name, draft, published) => {
    setAgent(draft, published);
    openLayout("/agents/a1/chat");
    const link = await screen.findByRole("link", { name: "Memory" });
    expect(link).toHaveAttribute("href", "/agents/a1/memory");
    expect(tabs().getAllByRole("link").map((item) => item.textContent)).toEqual(["Chat", "Traces", "Editor", "Memory"]);
  });

  it("the tab page sends an agent without memory to Chat (replace, so Back does not loop) and shows no inspector", async () => {
    setAgent(specOf());
    nav.search = "resource=alice";
    render(<MemoryTab agentId="a1" />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/agents/a1/chat"));
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.getByTestId("memory-tab-no-memory")).toBeInTheDocument();
    for (const id of ["memory-tab", "memory-working", "memory-forget-user", "memory-resource-input"]) expect(screen.queryByTestId(id)).toBeNull();
    expect(calls.filter((call) => call.path.includes("/memory/"))).toEqual([]);
    expect(listConversations).not.toHaveBeenCalled();
  });

  it("an agent with memory stays on the page: no redirect", async () => {
    render(<MemoryTab agentId="a1" />);
    await screen.findByTestId("memory-tab");
    expect(nav.replace).not.toHaveBeenCalled();
  });
});

describe("Memory tab: entering a user id", () => {
  const open = (value: string) => {
    fireEvent.change(screen.getByTestId("memory-resource-input"), { target: { value } });
    click("memory-resource-open");
  };

  it("every id of the table goes through the form as the Python validator says (after trimming)", async () => {
    render(<MemoryTab agentId="a1" />);
    await screen.findByTestId("memory-resource-input");
    for (const [id, valid] of [...ID_TABLE, [LONG_OK, true], [LONG_BAD, false]] as [string, boolean][]) {
      if (id.trim() !== id || id === "") continue; // trimming and empty are tested below
      nav.push.mockClear();
      open(id);
      if (valid) {
        expect(nav.push, JSON.stringify(id)).toHaveBeenCalledWith(routes.agentMemory("a1", { resource: id }));
        expect(screen.queryByTestId("memory-resource-error")).toBeNull();
      } else {
        expect(nav.push, JSON.stringify(id)).not.toHaveBeenCalled();
        expect(screen.getByTestId("memory-resource-error")).toBeInTheDocument();
      }
    }
  });

  it("trims, refuses empty input with its own message, and says which rule an id breaks", async () => {
    render(<MemoryTab agentId="a1" />);
    await screen.findByTestId("memory-resource-input");
    open("   ");
    expect(screen.getByTestId("memory-resource-error")).toHaveTextContent("Enter a user id.");
    open("..");
    expect(screen.getByTestId("memory-resource-error")).toHaveTextContent("dots only");
    open("a b");
    expect(screen.getByTestId("memory-resource-error")).toHaveTextContent("1 to 128 characters");
    expect(screen.getByTestId("memory-resource-input")).toHaveAttribute("aria-invalid", "true");
    expect(nav.push).not.toHaveBeenCalled();
    open("  alice@example.com  ");
    expect(nav.push).toHaveBeenCalledWith("/agents/a1/memory?resource=alice%40example.com");
    expect(screen.getByTestId("memory-resource-input")).toHaveValue("");
  });

  it("a bad ?resource= in the address is not looked up and has no Forget button", async () => {
    nav.search = "resource=" + encodeURIComponent("../secret");
    render(<MemoryTab agentId="a1" />);
    expect(await screen.findByRole("heading", { name: "Not a user id" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("1 to 128 characters");
    expect(screen.queryByTestId("memory-forget-user")).toBeNull();
    expect(calls.some((call) => call.path.includes("/memory/resources/"))).toBe(false);
  });
});

describe("Memory tab: a user's working memory", () => {
  beforeEach(() => {
    nav.search = "resource=alice";
    notes.set(USER_PATH, { content: "# User\n- Name: Alice", version: 1 });
  });

  it("views, edits with If-Match, clears and creates (version 0) a per-user note", async () => {
    render(<MemoryTab agentId="a1" />);
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("Name: Alice");
    expect(screen.getByTestId("memory-user-title")).toHaveTextContent("alice");
    expect(screen.getByRole("heading", { level: 2, name: "Working memory" })).toBeInTheDocument();
    click("memory-working-edit");
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "# User\n- Name: Alice B" } });
    click("memory-working-save");
    await waitFor(() => expect(screen.getByTestId("memory-working-content")).toHaveTextContent("Alice B"));
    expect(writes("PUT").map((call) => [call.path, call.headers.get("If-Match")])).toEqual([[USER_PATH, '"1"']]);
    expect(notes.get(USER_PATH)?.version).toBe(2);
    click("memory-working-clear");
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Clear working memory?" })).getByRole("button", { name: "Clear" }));
    await screen.findByTestId("memory-working-empty");
    expect(writes("DELETE").map((call) => [call.path, call.headers.get("If-Match")])).toEqual([[USER_PATH, '"2"']]);
    // Create starts from the agent's template, with If-Match 0.
    click("memory-working-create");
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue("# User\n- Name:");
    click("memory-working-save");
    await screen.findByTestId("memory-working-content");
    expect(writes("PUT").at(-1)?.headers.get("If-Match")).toBe('"0"');
  });

  it("a conflict on the tab shows the message and Reload", async () => {
    render(<MemoryTab agentId="a1" />);
    click((await screen.findByTestId("memory-working-edit")).getAttribute("data-testid")!);
    notes.set(USER_PATH, { content: "agent update", version: 4 });
    click("memory-working-save");
    expect(await screen.findByTestId("memory-working-conflict")).toHaveTextContent("working memory is at version 4, not the If-Match version 1");
    click("memory-working-reload");
    await waitFor(() => expect(screen.getByTestId("memory-working-textarea")).toHaveValue("agent update"));
  });

  it("schema mode on the tab edits JSON", async () => {
    const schema = specOf({ working: { scope: "resource", json_schema: { type: "object", properties: { name: { type: "string" } } } } });
    setAgent(schema);
    notes.set(USER_PATH, { content: { name: "Alice" }, version: 3 });
    render(<MemoryTab agentId="a1" />);
    click((await screen.findByTestId("memory-working-edit")).getAttribute("data-testid")!);
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: '{"name": "Alice B"}' } });
    click("memory-working-save");
    await waitFor(() => expect(screen.getByTestId("memory-working-content")).toHaveTextContent("Alice B"));
    expect(writes("PUT")[0].body).toEqual({ content: { name: "Alice B" } });
  });

  it("edits are checked against the latest PUBLISHED version: an unpublished working memory cannot be edited", async () => {
    setAgent(specOf(RESOURCE_WM), specOf({ observational: { model: { provider: "test", name: "o" } } }));
    render(<MemoryTab agentId="a1" />);
    expect(await screen.findByTestId("memory-working-content")).toBeInTheDocument();
    expect(screen.getByTestId("memory-working-edit")).toBeDisabled();
    expect(screen.getByTestId("memory-working-blocked")).toHaveTextContent("no working memory");
    expect(screen.getByTestId("memory-working-clear")).toBeEnabled();
  });

  it("the user list offers users with notes or conversations, and picking one goes to its address", async () => {
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c9", "bob")], next_before: null } as never);
    render(<MemoryTab agentId="a1" />);
    const options = await screen.findAllByTestId("memory-resource-option");
    expect(options.map((option) => option.textContent)).toEqual(["aliceWorking memory", "bobConversations"]);
    expect(options[0]).toHaveAttribute("aria-current", "true");
    fireEvent.click(options[1]);
    expect(nav.push).toHaveBeenCalledWith("/agents/a1/memory?resource=bob");
    expect(vi.mocked(listConversations).mock.calls[0][0]).toMatchObject({ agentId: "a1" });
  });
});

describe("Memory tab: Forget user", () => {
  const ID = "alice@example.com";
  const ENC = "alice%40example.com";
  beforeEach(() => {
    nav.search = `resource=${encodeURIComponent(ID)}`;
    notes.set(`/api/v1/agents/a1/memory/resources/${ENC}/working`, { content: "x", version: 1 });
  });
  const forgetCalls = () => writes("DELETE").filter((call) => /\/memory\/resources\/[^/]+$/.test(call.path));
  async function openDialog() {
    render(<MemoryTab agentId="a1" />);
    await screen.findByTestId("memory-working-content");
    click("memory-forget-user");
    return screen.findByRole("dialog", { name: `Forget ${ID}?` });
  }

  it.each([
    ["this agent (default)", "agent", false, `/api/v1/agents/a1/memory/resources/${ENC}?include_conversations=true`],
    ["this agent, keeping conversations", "agent", true, `/api/v1/agents/a1/memory/resources/${ENC}?include_conversations=false`],
    ["all agents", "all", false, `/api/v1/memory/resources/${ENC}?include_conversations=true`],
    ["all agents, keeping conversations", "all", true, `/api/v1/memory/resources/${ENC}?include_conversations=false`],
  ] as const)("%s: the exact request", async (_name, scope, keep, url) => {
    const dialog = await openDialog();
    expect(within(dialog).getByTestId("memory-forget-scope-agent")).toBeChecked();
    expect(within(dialog).getByTestId("memory-forget-keep-conversations")).not.toBeChecked();
    if (scope === "all") fireEvent.click(within(dialog).getByTestId("memory-forget-scope-all"));
    if (keep) fireEvent.click(within(dialog).getByTestId("memory-forget-keep-conversations"));
    expect(forgetCalls()).toHaveLength(0);
    fireEvent.click(within(dialog).getByTestId("memory-forget-confirm"));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/agents/a1/memory"));
    expect(forgetCalls()).toHaveLength(1);
    const call = forgetCalls()[0];
    expect(call.method).toBe("DELETE");
    expect(call.url.pathname + call.url.search).toBe(url);
    expect(call.url.searchParams.getAll("include_conversations")).toEqual([keep ? "false" : "true"]);
    expect(call.body).toBeUndefined();
  });

  it("the dialog's words follow the choices: the scope, and whether the conversations go too", async () => {
    const dialog = await openDialog();
    expect(dialog).toHaveTextContent("Support bot's working memory and recall index for this user, and also deletes this user's conversations with Support bot and their transcripts");
    fireEvent.click(within(dialog).getByTestId("memory-forget-scope-all"));
    expect(dialog).toHaveTextContent("every agent's working memory and recall index");
    fireEvent.click(within(dialog).getByTestId("memory-forget-keep-conversations"));
    expect(dialog).not.toHaveTextContent("also deletes this user's conversations");
    expect(dialog).toHaveTextContent("This cannot be undone.");
    expect(within(dialog).getByText("This agent (Support bot)")).toBeInTheDocument();
    expect(within(dialog).getByText("All agents")).toBeInTheDocument();
  });

  it("Cancel sends nothing; after a forget the note is gone and the page returns to the user list", async () => {
    let dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(forgetCalls()).toHaveLength(0);
    click("memory-forget-user");
    dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByTestId("memory-forget-confirm"));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/agents/a1/memory"));
    expect(notes.size).toBe(0);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("a busy 409 deletes nothing, keeps the dialog open with the API's message, and a retry works once the turn ends", async () => {
    busyConversations = ["c1"];
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByTestId("memory-forget-confirm"));
    expect(await within(dialog).findByTestId("memory-forget-error")).toHaveTextContent("a turn is still queued or running in 1 conversation(s) of this resource; nothing was deleted");
    expect(notes.size).toBe(1);
    expect(nav.replace).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: `Forget ${ID}?` })).toBeInTheDocument();
    expect(within(dialog).getByTestId("memory-forget-confirm")).toBeEnabled();
    busyConversations = [];
    fireEvent.click(within(dialog).getByTestId("memory-forget-confirm"));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/agents/a1/memory"));
    expect(forgetCalls()).toHaveLength(2);
    expect(notes.size).toBe(0);
  });

  it("the busy 409 on all agents says the same, and the scope choice survives the error", async () => {
    busyConversations = ["c1", "c2"];
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByTestId("memory-forget-scope-all"));
    fireEvent.click(within(dialog).getByTestId("memory-forget-confirm"));
    expect(await within(dialog).findByTestId("memory-forget-error")).toHaveTextContent("2 conversation(s)");
    expect(within(dialog).getByTestId("memory-forget-scope-all")).toBeChecked();
    expect(forgetCalls()[0].path).toBe(`/api/v1/memory/resources/${ENC}`);
  });
});

// ------------------------------------------------------------------------------- Act as user

describe("Act as user", () => {
  const chip = () => screen.getByTestId("composer-chip-act-as");
  async function actAs(id: string) {
    fireEvent.click(await screen.findByTestId("composer-chip-act-as"));
    fireEvent.change(await screen.findByTestId("act-as-input"), { target: { value: id } });
    fireEvent.click(screen.getByTestId("act-as-apply"));
    await waitFor(() => expect(screen.queryByTestId("act-as-input")).toBeNull());
  }
  const turnOptions = () => vi.mocked(startTurnWithOptions).mock.calls.map((args) => args[3]);

  it("only a NEW conversation gets the id; later turns, and the turn start itself, never carry it", async () => {
    render(<ChatTab agentId="a1" />);
    await actAs("alice");
    expect(createConversation).not.toHaveBeenCalled();
    await send("one");
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createConversation).mock.calls[0].slice(0, 1)).toEqual(["a1"]);
    expect(vi.mocked(createConversation).mock.calls[0][3]).toBe("alice");
    // Finish the turn, change the chip, and send a second turn in the same conversation.
    emit(ev(0, "run_started", { input: "one" }), answer(1), ev(2, "run_completed"));
    await waitFor(() => expect(box()).toBeEnabled());
    await actAs("bob");
    // The open conversation keeps alice; bob waits for the next new conversation.
    expect(chip()).toHaveAccessibleName("Act as user: alice (next: bob)");
    fireEvent.change(box(), { target: { value: "two" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(startTurnWithOptions).toHaveBeenCalledTimes(2));
    expect(createConversation).toHaveBeenCalledTimes(1);
    for (const options of turnOptions()) {
      expect(Object.keys(options)).not.toContain("resource_id");
      expect(JSON.stringify(options)).not.toMatch(/alice|bob/);
    }
  });

  it("an open conversation is fixed: the chip shows its user (or 'No user'), and the choice is kept for the next new one", async () => {
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c7", "carol"), conversation("c8", null)], next_before: null } as never);
    vi.mocked(getConversation).mockImplementation(async (id) => ({ ...conversation(id, id === "c7" ? "carol" : null), runs: [] }) as never);
    render(<ChatTab agentId="a1" conversationId="c7" />);
    await waitFor(() => expect(chip()).toHaveAccessibleName("Act as user: carol"));
    fireEvent.click(chip());
    expect(await screen.findByTestId("act-as-fixed")).toHaveTextContent("This conversation acts for carol and keeps it");
    fireEvent.change(screen.getByTestId("act-as-input"), { target: { value: "dave" } });
    fireEvent.click(screen.getByTestId("act-as-apply"));
    expect(chip()).toHaveAccessibleName("Act as user: carol (next: dave)");
    expect(createConversation).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(`${ACT_AS_PREFIX}${credentialScopeOf()}:a1`)).toBe("dave");
    // A conversation without a user says so.
    fireEvent.click(await screen.findByText("Chat c8"));
    await waitFor(() => expect(chip()).toHaveAccessibleName("Act as user: No user (next: dave)"));
    // New conversation: the chosen user is back, and the first send uses it.
    fireEvent.click(screen.getByRole("button", { name: /New conversation/ }));
    await waitFor(() => expect(chip()).toHaveAccessibleName("Act as user: dave"));
    await send("hi");
    await waitFor(() => expect(createConversation).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createConversation).mock.calls[0][3]).toBe("dave");
  });

  it("Act as yourself after a choice sends no resource_id again", async () => {
    render(<ChatTab agentId="a1" />);
    await actAs("alice");
    fireEvent.click(chip());
    fireEvent.click(await screen.findByTestId("act-as-clear"));
    await waitFor(() => expect(chip()).toHaveAccessibleName("Act as user: You"));
    expect(window.sessionStorage.length).toBe(0);
    await send("hi");
    await waitFor(() => expect(createConversation).toHaveBeenCalled());
    expect(vi.mocked(createConversation).mock.calls[0][3]).toBeNull();
  });

  it("the id is kept for this tab only: sessionStorage, per credential scope and agent, never localStorage", async () => {
    const first = render(<ChatTab agentId="a1" />);
    await actAs("erin@example.com");
    expect(window.sessionStorage.length).toBe(1);
    const key = window.sessionStorage.key(0)!;
    expect(key).toBe(`${ACT_AS_PREFIX}${credentialScopeOf()}:a1`);
    expect(key.startsWith("eap.studio.actAs.v1:")).toBe(true);
    expect(window.sessionStorage.getItem(key)).toBe("erin@example.com");
    expect(Object.keys(window.localStorage).filter((name) => window.localStorage.getItem(name)?.includes("erin"))).toEqual([]);
    expect(JSON.stringify(Object.entries(window.localStorage))).not.toContain("actAs");
    first.unmount();
    // Same tab, same agent: remembered. Another agent: not. A new tab (empty sessionStorage): the default.
    render(<ChatTab agentId="a1" />);
    await waitFor(() => expect(chip()).toHaveAccessibleName("Act as user: erin@example.com"));
    cleanup();
    window.sessionStorage.clear();
    render(<ChatTab agentId="a1" />);
    await waitFor(() => expect(chip()).toHaveAccessibleName("Act as user: You"));
  });

  it("is not offered when the agent has no memory, and is offered when only the latest version has it", async () => {
    setAgent(specOf());
    const view = render(<ChatTab agentId="a1" />);
    await screen.findByTestId("composer-chip-model");
    await waitFor(() => expect(api.versions.list).toHaveBeenCalled());
    expect(screen.queryByTestId("composer-chip-act-as")).toBeNull();
    view.unmount();
    setAgent(specOf(), specOf({ working: {} }));
    render(<ChatTab agentId="a1" />);
    expect(await screen.findByTestId("composer-chip-act-as")).toBeInTheDocument();
  });
});

describe("the conversation client (real lib/conversations)", () => {
  const real = () => vi.importActual<typeof import("@/lib/conversations")>("@/lib/conversations");

  it("createConversation sends resource_id only when there is one, deleteConversation DELETEs the encoded id", async () => {
    const { createConversation: create, deleteConversation: remove } = await real();
    override = (call) => (call.method === "POST" ? json({ id: "c1", agent_id: "a1", resource_id: "alice", title: null, turn_count: 0, created_at: T0, updated_at: T0 }, 201) : json(null, 204));
    await create("a1", null, "11111111-1111-4111-8111-111111111111", "alice");
    await create("a1", "T", "22222222-2222-4222-8222-222222222222", null);
    await create("a1", null, undefined, "");
    expect(writes("POST").map((call) => call.body)).toEqual([
      { title: null, conversation_id: "11111111-1111-4111-8111-111111111111", resource_id: "alice" },
      { title: "T", conversation_id: "22222222-2222-4222-8222-222222222222" },
      { title: null },
    ]);
    expect(writes("POST")[0].path).toBe("/api/v1/agents/a1/conversations");
    await remove("c 1/x");
    expect(writes("DELETE").map((call) => `${call.method} ${call.path}`)).toEqual(["DELETE /api/v1/conversations/c%201%2Fx"]);
  });

  it("deleteConversation errors carry the status and the API's message (409 busy, 404 gone)", async () => {
    const { deleteConversation: remove } = await real();
    override = () => json({ detail: "The latest turn (run r1) is still running; nothing was deleted. Retry when it finishes" }, 409);
    await expect(remove("c1")).rejects.toMatchObject({ status: 409, message: expect.stringContaining("nothing was deleted") });
    override = () => json({ detail: "Conversation not found" }, 404);
    await expect(remove("c1")).rejects.toMatchObject({ status: 404 });
  });
});

describe("Delete conversation", () => {
  let list: ReturnType<typeof conversation>[];
  beforeEach(() => {
    setAgent(specOf(FULL));
    window.localStorage.setItem(MEMORY_CARD_KEY, "open");
    list = [conversation("c7", "bob"), conversation("c8", "carol")];
    vi.mocked(listConversations).mockImplementation(async () => ({ items: [...list], next_before: null }) as never);
    vi.mocked(getConversation).mockImplementation(async (id) => ({ ...conversation(id, id === "c7" ? "bob" : "carol"), runs: [] }) as never);
    vi.mocked(deleteConversation).mockImplementation(async (id) => {
      list = list.filter((item) => item.id !== id);
    });
  });
  async function askDelete(title: string) {
    const rowButton = await screen.findByText(title);
    const trigger = within(rowButton.closest("li")!).getByTestId("conversation-actions");
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(await screen.findByTestId("conversation-delete"));
    return screen.findByRole("dialog", { name: "Delete this conversation?" });
  }
  const confirm = (dialog: HTMLElement) => fireEvent.click(within(dialog).getByRole("button", { name: "Delete conversation" }));

  it("the open conversation: the chat resets to a new conversation, the address moves, and the memory card has nothing to show", async () => {
    render(<ChatTab agentId="a1" conversationId="c7" />);
    await screen.findByTestId("memory-observations");
    const dialog = await askDelete("Chat c7");
    expect(dialog).toHaveTextContent("runs stay in Traces");
    expect(deleteConversation).not.toHaveBeenCalled();
    confirm(dialog);
    await waitFor(() => expect(deleteConversation).toHaveBeenCalledWith("c7"));
    await waitFor(() => expect(window.location.pathname).toBe("/agents/a1/chat"));
    await screen.findByTestId("memory-inspector-empty");
    expect(screen.getByTestId("chat-landing")).toBeInTheDocument();
    expect(screen.queryByText("Chat c7")).toBeNull();
    expect(screen.getByText("Chat c8")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("another conversation: deleted from the list, the open one stays open", async () => {
    render(<ChatTab agentId="a1" conversationId="c7" />);
    await screen.findByTestId("memory-observations");
    confirm(await askDelete("Chat c8"));
    await waitFor(() => expect(deleteConversation).toHaveBeenCalledWith("c8"));
    await waitFor(() => expect(screen.queryByText("Chat c8")).toBeNull());
    expect(screen.getByText("Chat c7")).toBeInTheDocument();
    expect(screen.getByTestId("memory-observations")).toBeInTheDocument();
    expect(screen.queryByTestId("memory-inspector-empty")).toBeNull();
  });

  it("Cancel keeps everything", async () => {
    render(<ChatTab agentId="a1" conversationId="c7" />);
    const dialog = await askDelete("Chat c7");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(deleteConversation).not.toHaveBeenCalled();
    expect(screen.getByText("Chat c7")).toBeInTheDocument();
  });

  it("a 409 (a turn is running) stays in the dialog with the API's words; the conversation and chat are untouched; a retry then works", async () => {
    vi.mocked(deleteConversation).mockRejectedValueOnce(Object.assign(new Error("The latest turn (run r1) is still running; nothing was deleted. Retry when it finishes"), { status: 409 }));
    render(<ChatTab agentId="a1" conversationId="c7" />);
    await screen.findByTestId("memory-observations");
    const dialog = await askDelete("Chat c7");
    confirm(dialog);
    expect(await within(dialog).findByTestId("confirm-dialog-error")).toHaveTextContent("The latest turn (run r1) is still running; nothing was deleted");
    expect(screen.getByRole("dialog", { name: "Delete this conversation?" })).toBeInTheDocument();
    expect(screen.getByText("Chat c7")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/agents/a1/chat/c7");
    expect(screen.queryByTestId("memory-inspector-empty")).toBeNull();
    confirm(dialog);
    await waitFor(() => expect(deleteConversation).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("Chat c7")).toBeNull());
    await screen.findByTestId("memory-inspector-empty");
  });

  it("a 404 (already gone) counts as deleted: no error, the row goes, the open chat resets", async () => {
    vi.mocked(deleteConversation).mockRejectedValueOnce(Object.assign(new Error("Conversation not found"), { status: 404 }));
    render(<ChatTab agentId="a1" conversationId="c7" />);
    await screen.findByTestId("memory-observations");
    confirm(await askDelete("Chat c7"));
    await waitFor(() => expect(screen.queryByText("Chat c7")).toBeNull());
    expect(screen.queryByTestId("confirm-dialog-error")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    await screen.findByTestId("memory-inspector-empty");
    await waitFor(() => expect(window.location.pathname).toBe("/agents/a1/chat"));
  });
});

// ------------------------------------------------------------------- the unsaved-edit confirms

describe("unsaved working memory edit: switching conversation", () => {
  const C7 = "/api/v1/conversations/c7/memory/working";
  beforeEach(() => {
    setAgent(specOf(FULL));
    window.localStorage.setItem(MEMORY_CARD_KEY, "open");
    notes.set(C7, { content: "c7 note", version: 2 });
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c7", "bob"), conversation("c8", "carol")], next_before: null } as never);
    vi.mocked(getConversation).mockImplementation(async (id) => ({ ...conversation(id, id === "c7" ? "bob" : "carol"), runs: [] }) as never);
  });
  async function dirtyEdit() {
    render(<ChatTab agentId="a1" conversationId="c7" />);
    fireEvent.click(await screen.findByTestId("memory-working-edit"));
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "unsaved text" } });
  }
  const discardDialog = () => screen.findByRole("dialog", { name: "Discard the working memory edit?" });

  it("opening another conversation asks; Stay keeps the edit, Discard edit switches", async () => {
    await dirtyEdit();
    fireEvent.click(screen.getByText("Chat c8"));
    let dialog = await discardDialog();
    expect(dialog).toHaveTextContent("Opening another conversation discards it.");
    expect(getConversation).not.toHaveBeenCalledWith("c8");
    fireEvent.click(within(dialog).getByRole("button", { name: "Stay" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue("unsaved text");
    expect(window.location.pathname).toBe("/agents/a1/chat/c7");
    fireEvent.click(screen.getByText("Chat c8"));
    dialog = await discardDialog();
    fireEvent.click(within(dialog).getByRole("button", { name: "Discard edit" }));
    await waitFor(() => expect(getConversation).toHaveBeenCalledWith("c8"));
    await waitFor(() => expect(window.location.pathname).toBe("/agents/a1/chat/c8"));
    expect(screen.queryByTestId("memory-working-textarea")).toBeNull();
    expect(writes("PUT")).toHaveLength(0);
  });

  it("New conversation asks too; the open conversation's own row does not", async () => {
    await dirtyEdit();
    fireEvent.click(screen.getByText("Chat c7"));
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /New conversation/ }));
    fireEvent.click(within(await discardDialog()).getByRole("button", { name: "Discard edit" }));
    await waitFor(() => expect(window.location.pathname).toBe("/agents/a1/chat"));
    await screen.findByTestId("memory-inspector-empty");
  });

  it("no unsaved edit, no question: a clean, cancelled or saved edit switches at once", async () => {
    notes.set("/api/v1/conversations/c8/memory/working", { content: "c8", version: 1 });
    render(<ChatTab agentId="a1" conversationId="c7" />);
    fireEvent.click(await screen.findByTestId("memory-working-edit"));
    // Opened but unchanged.
    fireEvent.click(screen.getByText("Chat c8"));
    await waitFor(() => expect(window.location.pathname).toBe("/agents/a1/chat/c8"));
    expect(screen.queryByRole("dialog")).toBeNull();
    // c8's own note first: Edit must not be c7's button still on screen.
    await waitFor(() => expect(screen.getByTestId("memory-working-content")).toHaveTextContent("c8"));
    fireEvent.click(screen.getByTestId("memory-working-edit"));
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "changed" } });
    fireEvent.click(screen.getByTestId("memory-working-save"));
    await screen.findByTestId("memory-working-content");
    fireEvent.click(screen.getByText("Chat c7"));
    await waitFor(() => expect(window.location.pathname).toBe("/agents/a1/chat/c7"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closing the tab asks while the edit is unsaved (beforeunload), and not before or after", async () => {
    await dirtyEdit();
    const unload = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(unload()).toBe(true);
    fireEvent.click(screen.getByTestId("memory-working-cancel"));
    await waitFor(() => expect(unload()).toBe(false));
  });
});

describe("unsaved working memory edit: switching user, g shortcuts, palette and links", () => {
  beforeEach(() => {
    nav.path = "/agents/a1/memory";
    nav.search = "resource=alice";
    notes.set(USER_PATH, { content: "alice note", version: 1 });
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c9", "bob")], next_before: null } as never);
  });
  async function dirtyTab() {
    render(
      <KeyboardHarness>
        <a href="/metrics">Metrics link</a>
        <MemoryTab agentId="a1" />
      </KeyboardHarness>,
    );
    fireEvent.click(await screen.findByTestId("memory-working-edit"));
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "unsaved" } });
  }
  const dialogOf = () => screen.findByRole("dialog", { name: "Discard the working memory edit?" });
  const stay = async (dialog: HTMLElement) => {
    fireEvent.click(within(dialog).getByRole("button", { name: "Stay" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue("unsaved");
  };

  it("picking another user asks; Stay keeps the edit, Discard edit opens the user", async () => {
    await dirtyTab();
    fireEvent.click(await screen.findByText("bob"));
    const dialog = await dialogOf();
    expect(dialog).toHaveTextContent("Opening another user discards it.");
    await stay(dialog);
    fireEvent.click(screen.getByText("bob"));
    fireEvent.click(within(await dialogOf()).getByRole("button", { name: "Discard edit" }));
    expect(nav.push).toHaveBeenCalledWith("/agents/a1/memory?resource=bob");
  });

  it("a typed user id asks as well, and the user already open does not", async () => {
    await dirtyTab();
    fireEvent.change(screen.getByTestId("memory-resource-input"), { target: { value: "alice" } });
    click("memory-resource-open");
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.change(screen.getByTestId("memory-resource-input"), { target: { value: "carol" } });
    click("memory-resource-open");
    fireEvent.click(within(await dialogOf()).getByRole("button", { name: "Discard edit" }));
    expect(nav.push).toHaveBeenCalledWith("/agents/a1/memory?resource=carol");
  });

  it("the g shortcuts ask first, with the memory words", async () => {
    await dirtyTab();
    pressKeys("g", "t");
    const dialog = await dialogOf();
    expect(dialog).toHaveTextContent("The working memory edit is not saved. Leaving discards it.");
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    await stay(dialog);
    pressKeys("g", "t");
    fireEvent.click(within(await dialogOf()).getByRole("button", { name: "Discard edit" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(routes.traces()));
  });

  it("the command palette asks first", async () => {
    await dirtyTab();
    const navigate = async () => {
      pressKey("k", { metaKey: true });
      const input = await screen.findByTestId("command-palette-input");
      fireEvent.change(input, { target: { value: "Metrics" } });
      fireEvent.keyDown(input, { key: "Enter" });
    };
    await navigate();
    await stay(await dialogOf());
    await navigate();
    fireEvent.click(within(await dialogOf()).getByRole("button", { name: "Discard edit" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(expect.stringMatching(/^\/metrics/)));
  });

  it("a link to another page asks, and follows after Discard edit", async () => {
    await dirtyTab();
    fireEvent.click(screen.getByRole("link", { name: "Metrics link" }));
    await stay(await dialogOf());
    fireEvent.click(screen.getByRole("link", { name: "Metrics link" }));
    fireEvent.click(within(await dialogOf()).getByRole("button", { name: "Discard edit" }));
    expect(nav.push).toHaveBeenCalledWith("/metrics");
  });

  it("a clean edit, a saved edit and Cancel do not ask", async () => {
    await dirtyTab();
    fireEvent.click(screen.getByTestId("memory-working-cancel"));
    pressKeys("g", "t");
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(routes.traces()));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("unsaved working memory edit in the chat's memory card: g shortcuts and palette", () => {
  beforeEach(() => {
    nav.path = "/agents/a1/chat/c7";
    setAgent(specOf(FULL));
    window.localStorage.setItem(MEMORY_CARD_KEY, "open");
    notes.set("/api/v1/conversations/c7/memory/working", { content: "c7 note", version: 2 });
    vi.mocked(getConversation).mockResolvedValue({ ...conversation("c7", "bob"), runs: [] } as never);
  });
  const dialogOf = () => screen.findByRole("dialog", { name: "Discard the working memory edit?" });

  it("ask first, name the memory edit, and go only after Discard edit", async () => {
    render(
      <KeyboardHarness>
        <ChatTab agentId="a1" conversationId="c7" />
      </KeyboardHarness>,
    );
    fireEvent.click(await screen.findByTestId("memory-working-edit"));
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "unsaved" } });
    pressKeys("g", "t");
    let dialog = await dialogOf();
    fireEvent.click(within(dialog).getByRole("button", { name: "Stay" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(nav.push).not.toHaveBeenCalled();
    pressKey("k", { metaKey: true });
    const input = await screen.findByTestId("command-palette-input");
    fireEvent.change(input, { target: { value: "Metrics" } });
    fireEvent.keyDown(input, { key: "Enter" });
    dialog = await dialogOf();
    fireEvent.click(within(dialog).getByRole("button", { name: "Discard edit" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(expect.stringMatching(/^\/metrics/)));
  });
});

// ------------------------------------------------------------------------------------- layout

/** A matchMedia where exactly the listed queries match. */
function stubMedia(matching: string[]) {
  const original = window.matchMedia;
  window.matchMedia = vi.fn((query: string) => ({
    matches: matching.includes(query), media: query, onchange: null, addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

describe("layout: phone width", () => {
  const SHEET = "(max-width: 639.98px)";
  it("Act as user is a popover on a desktop window and a Sheet below 640 px", async () => {
    let restore = stubMedia([]);
    const onChange = vi.fn();
    const wide = render(<ComposerActAsChip value="" onChange={onChange} />);
    fireEvent.click(screen.getByTestId("composer-chip-act-as"));
    expect(await screen.findByTestId("act-as-popover")).toBeInTheDocument();
    expect(screen.queryByTestId("act-as-sheet")).toBeNull();
    wide.unmount();
    restore();
    restore = stubMedia([SHEET]);
    try {
      render(<ComposerActAsChip value="" onChange={onChange} />);
      fireEvent.click(screen.getByTestId("composer-chip-act-as"));
      const sheet = await screen.findByRole("dialog", { name: "Act as user" });
      expect(sheet).toHaveAttribute("data-testid", "act-as-sheet");
      expect(screen.queryByTestId("act-as-popover")).toBeNull();
      fireEvent.change(within(sheet).getByTestId("act-as-input"), { target: { value: "frank" } });
      fireEvent.click(within(sheet).getByTestId("act-as-apply"));
      expect(onChange).toHaveBeenCalledWith("frank");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    } finally {
      restore();
    }
  });

  it("the chip is capped and truncates a long id; the sheet validates like the popover", async () => {
    const restore = stubMedia([SHEET]);
    try {
      render(<ComposerActAsChip value={"x".repeat(128)} onChange={vi.fn()} />);
      const chip = screen.getByTestId("composer-chip-act-as");
      expect(chip.className).toMatch(/max-w-\[11rem\]/);
      expect(chip.className).toMatch(/min-w-0/);
      expect(screen.getByTestId("composer-chip-label").className).toMatch(/truncate/);
      fireEvent.click(chip);
      fireEvent.change(await screen.findByTestId("act-as-input"), { target: { value: "bad id" } });
      expect(screen.getByTestId("act-as-error")).toHaveTextContent("1 to 128 characters");
      expect(screen.getByTestId("act-as-apply")).toBeDisabled();
    } finally {
      restore();
    }
  });

  /** Fixed widths that are not responsive: what would push a 360 px phone sideways. */
  const fixedWidths = (root: Element) =>
    [root, ...Array.from(root.querySelectorAll("*"))].flatMap((element) =>
      String(element.getAttribute("class") ?? "").split(/\s+/).filter((name) => /^(min-w|w)-(\d+|\[\d+(px|rem)\])$/.test(name) && !/^(w|min-w)-(0|1|2|3|4|5|6|8|10|12)$/.test(name)),
    );

  it("the Memory tab stacks on a phone, side by side from lg; its parts shrink and wrap", async () => {
    nav.search = "resource=alice";
    notes.set(USER_PATH, { content: "x", version: 1 });
    render(<MemoryTab agentId="a1" />);
    const tab = await screen.findByTestId("memory-tab");
    expect(tab.className).toMatch(/\bflex-col\b/);
    expect(tab.className).toMatch(/\blg:flex-row\b/);
    expect(tab.className).toMatch(/\bmin-w-0\b/);
    expect(tab.className).toMatch(/\bw-full\b/);
    const [users, user] = Array.from(tab.querySelectorAll(":scope > section"));
    expect(users.className).toMatch(/\blg:w-72\b/);
    expect(users.className).not.toMatch(/(^|\s)w-72\b/);
    expect(user.className).toMatch(/\bmin-w-0\b/);
    expect(screen.getByTestId("memory-user-title").className).toMatch(/truncate/);
    expect(screen.getByTestId("memory-resource-input").className).toMatch(/min-w-0/);
    await screen.findByTestId("memory-working-content");
    expect(screen.getByTestId("memory-working-content").className).toMatch(/overflow-auto/);
    expect(screen.getByTestId("memory-working-content").className).toMatch(/break-words/);
    click("memory-working-edit");
    expect(screen.getByTestId("memory-working-textarea").className).toMatch(/w-full min-w-0/);
    expect(fixedWidths(tab)).toEqual([]);
  });

  it("the card's inspector and observations shrink and wrap too", async () => {
    observations.c1 = [row("o1", 1, 4, { content: "a-very-long-unbroken-token".repeat(10) }), marker("m1", 5, 8)];
    const spec = specOf(FULL);
    const activity = { recall: { status: "none" as const }, revision: 0, updating: false };
    window.localStorage.setItem(MEMORY_CARD_KEY, "open");
    render(<MemoryCard spec={spec} inspector={{ agentId: "a1", conversationId: "c1", resourceId: "alice", publishedSpec: spec, activity }} />);
    await screen.findAllByTestId("memory-observation-row");
    const card = screen.getByTestId("memory-card");
    expect(screen.getByTestId("memory-inspector").className).toMatch(/min-w-0/);
    expect(screen.getByTestId("memory-observations").className).toMatch(/min-w-0/);
    for (const element of screen.getAllByTestId("memory-observation-row")) expect(element.className).toMatch(/min-w-0/);
    expect(screen.getAllByTestId("memory-observation-row")[0].querySelector("p")?.className).toMatch(/break-words/);
    expect(card.className).toMatch(/max-h-\[60%\]/);
    expect(fixedWidths(card)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------- axe, themes

describe.each(["light", "dark"] as const)("accessibility (%s theme)", (theme) => {
  const PAGE = { rules: { "landmark-one-main": { enabled: true }, region: { enabled: true } } };
  beforeEach(() => {
    window.localStorage.setItem("studio.theme", theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.lang = "en";
    media = mockMatchMedia(true);
  });
  afterEach(() => {
    document.documentElement.removeAttribute("lang");
    document.documentElement.style.colorScheme = "";
  });
  const settle = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  const oneH1 = () => {
    expect(document.querySelectorAll("main, [role='main']")).toHaveLength(1);
    expect(within(screen.getByRole("main")).getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(within(screen.getByRole("main")).getByRole("heading", { level: 1 })).toHaveTextContent("Support bot");
    expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark");
  };

  it("the Memory tab: one h1, no violations; with a conflict showing, and with Forget user open", async () => {
    nav.path = "/agents/a1/memory";
    nav.search = "resource=alice";
    notes.set(USER_PATH, { content: "# User\n- Name: Alice", version: 1 });
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c9", "bob")], next_before: null } as never);
    render(
      <FrameHarness>
        <AgentLayout agentId="a1">
          <MemoryTab agentId="a1" />
        </AgentLayout>
      </FrameHarness>,
    );
    await screen.findByTestId("memory-working-content");
    await screen.findAllByTestId("memory-resource-option");
    await settle();
    oneH1();
    await expectNoAxeViolations(document, PAGE);
    click("memory-working-edit");
    notes.set(USER_PATH, { content: "newer", version: 3 });
    click("memory-working-save");
    await screen.findByTestId("memory-working-conflict");
    fireEvent.change(screen.getByTestId("memory-resource-input"), { target: { value: "bad id" } });
    click("memory-resource-open");
    await screen.findByTestId("memory-resource-error");
    await expectNoAxeViolations(document, PAGE);
    fireEvent.click(screen.getByTestId("memory-working-cancel"));
    click("memory-forget-user");
    const dialog = await screen.findByRole("dialog", { name: "Forget alice?" });
    busyConversations = ["c1"];
    fireEvent.click(within(dialog).getByTestId("memory-forget-confirm"));
    await within(dialog).findByTestId("memory-forget-error");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("the Chat tab with the memory card open (note, observations incl. a marker, recall), editing, and the Act as user popover", async () => {
    setAgent(specOf(FULL));
    window.localStorage.setItem(MEMORY_CARD_KEY, "open");
    notes.set("/api/v1/conversations/c7/memory/working", { content: "c7 note", version: 2 });
    observations.c7 = [row("o1", 1, 4, { conversation_id: "c7" }), marker("m1", 5, 8)];
    vi.mocked(listConversations).mockResolvedValue({ items: [conversation("c7", "bob")], next_before: null } as never);
    vi.mocked(getConversation).mockResolvedValue({ ...conversation("c7", "bob"), runs: [] } as never);
    render(
      <FrameHarness>
        <AgentLayout agentId="a1">
          <ChatTab agentId="a1" conversationId="c7" />
        </AgentLayout>
      </FrameHarness>,
    );
    await screen.findAllByTestId("memory-observation-row");
    await screen.findByTestId("memory-working-content");
    await settle();
    oneH1();
    await expectNoAxeViolations(document, PAGE);
    click("memory-working-edit");
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "x" } });
    notes.set("/api/v1/conversations/c7/memory/working", { content: "newer", version: 5 });
    click("memory-working-save");
    await screen.findByTestId("memory-working-conflict");
    await expectNoAxeViolations(document, PAGE);
    click("memory-observations-delete-all");
    await screen.findByRole("dialog", { name: "Delete all observations?" });
    await expectNoAxeViolations(document.body);
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByTestId("composer-chip-act-as"));
    await screen.findByTestId("act-as-popover");
    fireEvent.change(screen.getByTestId("act-as-input"), { target: { value: "bad id" } });
    await screen.findByTestId("act-as-error");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("the Act as user Sheet below 640 px has no violations", async () => {
    const restore = stubMedia(["(max-width: 639.98px)"]);
    try {
      render(<ComposerActAsChip value="alice" onChange={vi.fn()} conversationUser="bob" />);
      fireEvent.click(screen.getByTestId("composer-chip-act-as"));
      await screen.findByRole("dialog", { name: "Act as user" });
      await expectNoAxeViolations(document.body);
    } finally {
      restore();
    }
  }, AXE_TIMEOUT_MS);
});
