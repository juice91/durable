import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  MemoryApiError,
  blockedByExpired,
  deleteAllObservations,
  deleteObservation,
  deleteWorkingMemory,
  forgetResource,
  getWorkingMemory,
  listAgentWorkingMemories,
  listObservations,
  memoryWrites,
  newestObservation,
  noteText,
  observerProgress,
  parseNoteText,
  postTurnReported,
  putWorkingMemory,
  recallSummary,
  reflectionDue,
  resourceIdProblem,
  type Observation,
} from "./memory";
import { createConversation, deleteConversation } from "./conversations";

type Call = { url: URL; init: RequestInit };
let calls: Call[];
let answer: (url: URL, init: RequestInit) => Response;

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  calls = [];
  answer = () => json({});
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      calls.push({ url, init });
      return answer(url, init);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const header = (call: Call, name: string) => new Headers(call.init.headers).get(name);

const obs = (id: string, from: number, to: number, extra: Partial<Observation> = {}): Observation => ({
  id,
  agent_id: "a1",
  conversation_id: "c1",
  kind: "observation",
  covers_from_turn: from,
  covers_to_turn: to,
  content: `notes ${id}`,
  approx_tokens: 40,
  run_id: "r1",
  superseded_by: null,
  created_at: "2026-11-14T10:00:00Z",
  ...extra,
});

describe("resource ids (eap_run_store.validate_resource_id)", () => {
  it.each(["alice", "alice@example.com", "org:42", "a.b-c_d", "x".repeat(128)])("accepts %s", (id) => {
    expect(resourceIdProblem(id)).toBeNull();
  });
  it.each(["has space", "slash/here", "x".repeat(129), "émile", ""])("refuses %s", (id) => {
    expect(resourceIdProblem(id)).toMatch(/1 to 128 characters/);
  });
  it.each([".", "..", "..."])("refuses the dot-only id %s", (id) => {
    expect(resourceIdProblem(id)).toMatch(/dots only/);
  });
});

describe("working memory requests", () => {
  it("addresses the conversation note or the agent's per-user note", async () => {
    answer = () => json({ version: 3, content: "x", format: "markdown" });
    await getWorkingMemory({ scope: "conversation", conversationId: "c1" });
    await getWorkingMemory({ scope: "resource", agentId: "a1", resourceId: "alice@example.com" });
    expect(calls.map((c) => c.url.pathname)).toEqual([
      "/api/v1/conversations/c1/memory/working",
      "/api/v1/agents/a1/memory/resources/alice%40example.com/working",
    ]);
  });

  it("answers null for a missing note (404)", async () => {
    answer = () => json({ detail: "Working memory not found" }, 404);
    await expect(getWorkingMemory({ scope: "conversation", conversationId: "c1" })).resolves.toBeNull();
  });

  it("PUT sends the content and If-Match with the edit's base version (0: create only)", async () => {
    answer = (_url, init) => json({ version: 4, content: JSON.parse(String(init.body)).content, format: "json" });
    await putWorkingMemory({ scope: "conversation", conversationId: "c1" }, { name: "Ada" }, 3);
    await putWorkingMemory({ scope: "conversation", conversationId: "c1" }, "# New", 0);
    expect(calls[0].init.method).toBe("PUT");
    expect(header(calls[0], "If-Match")).toBe('"3"');
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ content: { name: "Ada" } });
    expect(header(calls[1], "If-Match")).toBe('"0"');
  });

  it("a version conflict is a 409 MemoryApiError carrying the API's message", async () => {
    answer = () => json({ detail: "working memory is at version 5, not the If-Match version 3; reload it and apply the edit again" }, 409);
    const error = await putWorkingMemory({ scope: "conversation", conversationId: "c1" }, "x", 3).catch((e) => e);
    expect(error).toBeInstanceOf(MemoryApiError);
    expect(error.status).toBe(409);
    expect(error.message).toContain("version 5");
  });

  it("a 422 list joins its messages", async () => {
    answer = () => json({ detail: [{ loc: ["body", "content", "name"], msg: "name: expected string" }] }, 422);
    await expect(putWorkingMemory({ scope: "conversation", conversationId: "c1" }, {}, 1)).rejects.toMatchObject({ status: 422, message: "name: expected string" });
  });

  it("DELETE sends If-Match when given", async () => {
    answer = () => json(null, 204);
    await deleteWorkingMemory({ scope: "resource", agentId: "a1", resourceId: "bob" }, 2);
    await deleteWorkingMemory({ scope: "resource", agentId: "a1", resourceId: "bob" });
    expect(calls[0].init.method).toBe("DELETE");
    expect(header(calls[0], "If-Match")).toBe('"2"');
    expect(header(calls[1], "If-Match")).toBeNull();
  });

  it("lists the agent's per-user notes", async () => {
    answer = () => json({ items: [{ scope_key: "alice" }] });
    await expect(listAgentWorkingMemories("a1", "resource")).resolves.toEqual([{ scope_key: "alice" }]);
    expect(calls[0].url.pathname).toBe("/api/v1/agents/a1/memory/working");
    expect(calls[0].url.searchParams.get("scope")).toBe("resource");
  });
});

describe("observations", () => {
  it("list, delete one and delete all", async () => {
    answer = (url, init) =>
      init.method === "DELETE" ? (url.pathname.endsWith("/observations") ? json({ deleted: 3 }) : json(null, 204)) : json({ items: [obs("o1", 1, 4)], covered_to_turn: 4 });
    await expect(listObservations("c1")).resolves.toMatchObject({ covered_to_turn: 4 });
    await deleteObservation("c1", "o1");
    await expect(deleteAllObservations("c1")).resolves.toBe(3);
    expect(calls.map((c) => `${c.init.method ?? "GET"} ${c.url.pathname}`)).toEqual([
      "GET /api/v1/conversations/c1/memory/observations",
      "DELETE /api/v1/conversations/c1/memory/observations/o1",
      "DELETE /api/v1/conversations/c1/memory/observations",
    ]);
  });

  it("the newest is the active row with the highest covers_to_turn, an expired marker included", () => {
    const rows = [obs("o1", 1, 4), obs("o2", 5, 8, { expired: true, content: "", approx_tokens: 0 }), obs("old", 1, 9, { superseded_by: "r" })];
    expect(newestObservation(rows)?.id).toBe("o2");
    expect(blockedByExpired(rows, rows[0])).toBe(true);
    expect(blockedByExpired(rows, rows[1])).toBe(false);
    expect(blockedByExpired([obs("o1", 1, 4), obs("o3", 5, 8)], obs("o1", 1, 4))).toBe(false);
    expect(newestObservation([])).toBeNull();
  });
});

describe("forget a user", () => {
  it("this agent or all agents, with include_conversations", async () => {
    answer = () => json({ resource_id: "alice", agent_id: null, working_memories_deleted: 1, message_embeddings_deleted: 2, conversations_deleted: ["c1"], busy_conversations: [] });
    await forgetResource("alice", { agentId: "a1", includeConversations: true });
    await forgetResource("alice", { agentId: null, includeConversations: false });
    expect(calls[0].init.method).toBe("DELETE");
    expect(calls[0].url.pathname).toBe("/api/v1/agents/a1/memory/resources/alice");
    expect(calls[0].url.searchParams.get("include_conversations")).toBe("true");
    expect(calls[1].url.pathname).toBe("/api/v1/memory/resources/alice");
    expect(calls[1].url.searchParams.get("include_conversations")).toBe("false");
  });

  it("a busy 409 carries the busy conversations", async () => {
    answer = () => json({ detail: "a turn is still queued or running in 1 conversation(s) of this resource; nothing was deleted. Retry when the turns finish", busy_conversations: ["c9"] }, 409);
    const error = await forgetResource("alice", { includeConversations: true }).catch((e) => e);
    expect(error).toMatchObject({ status: 409, busyConversations: ["c9"] });
    expect(error.message).toContain("nothing was deleted");
  });
});

describe("conversations (RM.18 additions)", () => {
  it("createConversation sends resource_id only when acting as a user", async () => {
    answer = () => json({ id: "c1" }, 201);
    await createConversation("a1", null, "cid-1");
    await createConversation("a1", null, "cid-2", "alice");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ title: null, conversation_id: "cid-1" });
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ title: null, conversation_id: "cid-2", resource_id: "alice" });
  });

  it("deleteConversation sends DELETE and throws the API's status", async () => {
    answer = () => json(null, 204);
    await deleteConversation("c1");
    expect(calls[0].init.method).toBe("DELETE");
    expect(calls[0].url.pathname).toBe("/api/v1/conversations/c1");
    answer = () => json({ detail: "The latest turn (run r1) is still running; nothing was deleted. Retry when it finishes" }, 409);
    await expect(deleteConversation("c1")).rejects.toMatchObject({ status: 409 });
  });
});

describe("note text", () => {
  it("shows Markdown as is and an object as indented JSON", () => {
    expect(noteText("# User")).toBe("# User");
    expect(noteText({ name: "Ada" })).toBe('{\n  "name": "Ada"\n}');
    expect(noteText(null)).toBe("");
  });
  it("parses JSON mode into an object, and refuses non-objects", () => {
    expect(parseNoteText('{"a": 1}', "json")).toEqual({ content: { a: 1 } });
    expect(parseNoteText("[1]", "json")).toMatchObject({ error: expect.stringContaining("JSON object") });
    expect(parseNoteText("{oops", "json")).toMatchObject({ error: expect.stringContaining("Not valid JSON") });
    expect(parseNoteText("# Any text", "markdown")).toEqual({ content: "# Any text" });
  });
});

describe("what a turn's events say about memory", () => {
  const ev = (seq: number, type: string, data: Record<string, unknown> = {}) => ({ seq, type, data });

  it("recall: none, done with counts, or failed", () => {
    expect(recallSummary([ev(0, "run_started")])).toEqual({ status: "none" });
    expect(recallSummary([ev(1, "embedding", { stage: "recall", hits: 5, turns: 2, groups: 1, messages: 6 })])).toEqual({ status: "done", hits: 5, turns: 2, groups: 1, messages: 6 });
    expect(recallSummary([ev(1, "embedding", { stage: "recall", status: "failed", error_code: "embedding_timeout" })])).toEqual({ status: "failed", errorCode: "embedding_timeout" });
    // The index event is not recall.
    expect(recallSummary([ev(9, "embedding", { stage: "index" })])).toEqual({ status: "none" });
  });

  it("counts memory writes", () => {
    expect(memoryWrites([ev(1, "memory_updated", { tool_call_id: "t" }), ev(2, "tool_result"), ev(3, "memory_observe_failed")])).toBe(2);
  });

  it("post-turn steps report: the observer with observational memory, else the recall index", () => {
    const both = { observational: true, semanticRecall: true };
    expect(postTurnReported([ev(5, "embedding", { stage: "index" })], both)).toBe(false);
    expect(postTurnReported([ev(6, "memory_updated", { stage: "observe" })], both)).toBe(true);
    expect(postTurnReported([ev(6, "memory_updated", { stage: "reflect" })], both)).toBe(true);
    expect(postTurnReported([ev(6, "memory_observe_failed", { stage: "observe" })], both)).toBe(true);
    // A working memory write during the turn is not the observer.
    expect(postTurnReported([ev(3, "memory_updated", { tool_call_id: "t" })], both)).toBe(false);
    expect(postTurnReported([ev(5, "embedding", { stage: "index" })], { observational: false, semanticRecall: true })).toBe(true);
    expect(postTurnReported([], { observational: false, semanticRecall: false })).toBe(true);
  });

  it("with a reflection due, a successful observe is not the end; the reflect stage (done or failed) is", () => {
    const f = { observational: true, semanticRecall: false };
    const observed = [ev(6, "memory_updated", { stage: "observe" })];
    expect(observerProgress(observed)).toBe("observed");
    expect(postTurnReported(observed, f, { awaitReflection: true })).toBe(false);
    expect(postTurnReported([...observed, ev(7, "memory_updated", { stage: "reflect" })], f, { awaitReflection: true })).toBe(true);
    expect(postTurnReported([...observed, ev(7, "memory_observe_failed", { stage: "reflect" })], f, { awaitReflection: true })).toBe(true);
    // A failed observe ends the post-turn steps: no reflection follows it.
    expect(postTurnReported([ev(6, "memory_observe_failed", { stage: "observe" })], f, { awaitReflection: true })).toBe(true);
    expect(observerProgress([])).toBe("pending");
  });

  it("reflectionDue mirrors the worker: two or more active text rows over the threshold", () => {
    const row = (tokens: number, extra: Partial<Observation> = {}): Observation => ({
      id: String(tokens), agent_id: "a", conversation_id: "c", kind: "observation", covers_from_turn: 1, covers_to_turn: 1,
      content: "x", approx_tokens: tokens, run_id: null, superseded_by: null, created_at: "", ...extra,
    });
    expect(reflectionDue([row(5000), row(4000)], 8000)).toBe(true);
    expect(reflectionDue([row(4000), row(4000)], 8000)).toBe(false); // not more than
    expect(reflectionDue([row(9000)], 8000)).toBe(false);
    expect(reflectionDue([row(5000), row(4000, { superseded_by: "z" })], 8000)).toBe(false);
    expect(reflectionDue([row(5000), row(4000, { expired: true, content: "" })], 8000)).toBe(false);
  });
});
