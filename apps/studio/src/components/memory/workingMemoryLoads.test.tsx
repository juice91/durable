// One working memory read per note and refresh (the milestone browser check: a user without a note
// got three 404s per chat refresh). The shared loader (lib/workingMemoryCache) and the editor's use.
import React, { StrictMode } from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkingMemoryEditor, targetKeyOf } from "./WorkingMemoryEditor";
import { FRESH_MS, primeWorkingMemory, resetWorkingMemoryCache, sharedWorkingMemory } from "@/lib/workingMemoryCache";
import { conflictMessage, forgetResource } from "@/lib/memory";
import { deleteConversation } from "@/lib/conversations";

let gets: string[];
let notes: Map<string, unknown>;

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const T0 = "2026-11-14T10:00:00Z";
const PATH = "/api/v1/conversations/c1/memory/working";
const note = (content: string, version: number) => ({
  agent_id: "a1", scope: "conversation", scope_key: "c1", format: "markdown", content, version,
  updated_by_run_id: null, created_at: T0, updated_at: T0,
});

beforeEach(() => {
  gets = [];
  notes = new Map();
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      const method = init.method ?? "GET";
      if (method === "GET") {
        gets.push(url.pathname);
        const found = notes.get(url.pathname);
        return found ? json(found) : json({ detail: "working memory not found" }, 404);
      }
      if (method === "DELETE") {
        // Forget a user (this agent's or every agent's notes of the resource): a ForgetResult.
        const forget = /^\/api\/v1\/(?:agents\/[^/]+\/)?memory\/resources\/([^/]+)$/.exec(url.pathname);
        if (forget) {
          let removed = 0;
          for (const key of [...notes.keys()]) if (key.endsWith(`/memory/resources/${forget[1]}/working`)) removed += notes.delete(key) ? 1 : 0;
          return json({ resource_id: forget[1], agent_id: null, working_memories_deleted: removed, message_embeddings_deleted: 0, conversations_deleted: [], busy_conversations: [] });
        }
        // Delete a conversation: its conversation-scoped note goes with it.
        const conversation = /^\/api\/v1\/conversations\/([^/]+)$/.exec(url.pathname);
        if (conversation) {
          notes.delete(`/api/v1/conversations/${conversation[1]}/memory/working`);
          return json(null, 204);
        }
        notes.delete(url.pathname);
        return json(null, 204);
      }
      return json({ detail: "unexpected" }, 500);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const target = { scope: "conversation" as const, conversationId: "c1" };

function Editor({ refreshKey = 0 }: { refreshKey?: number }) {
  return <WorkingMemoryEditor target={target} format="markdown" starter="# Notes" refreshKey={refreshKey} />;
}

describe("sharedWorkingMemory", () => {
  it("shares an in-flight read and reuses a settled one for FRESH_MS", async () => {
    let now = 1_000;
    const load = vi.fn(async () => "note");
    const options = { now: () => now };
    const [a, b] = await Promise.all([sharedWorkingMemory("k", load, options), sharedWorkingMemory("k", load, options)]);
    expect([a, b]).toEqual(["note", "note"]);
    expect(load).toHaveBeenCalledTimes(1);
    now += FRESH_MS - 1;
    await sharedWorkingMemory("k", load, options);
    expect(load).toHaveBeenCalledTimes(1);
    now += 1;
    await sharedWorkingMemory("k", load, options);
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("force reads again; another key is its own read", async () => {
    const load = vi.fn(async () => null);
    await sharedWorkingMemory("k", load);
    await sharedWorkingMemory("k", load, { force: true });
    await sharedWorkingMemory("other", load);
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("does not keep a failed read", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("down")).mockResolvedValueOnce("ok");
    await expect(sharedWorkingMemory("k", load)).rejects.toThrow("down");
    await expect(sharedWorkingMemory("k", load)).resolves.toBe("ok");
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("a primed value answers without a read until forced", async () => {
    primeWorkingMemory("k", null);
    const load = vi.fn(async () => "fresh");
    await expect(sharedWorkingMemory("k", load)).resolves.toBeNull();
    expect(load).not.toHaveBeenCalled();
    await expect(sharedWorkingMemory("k", load, { force: true })).resolves.toBe("fresh");
  });

  it("resetWorkingMemoryCache forgets everything", async () => {
    primeWorkingMemory("k", "old");
    resetWorkingMemoryCache();
    await expect(sharedWorkingMemory("k", async () => "new")).resolves.toBe("new");
  });
});

describe("WorkingMemoryEditor reads", () => {
  it("StrictMode mount: one GET, and the 404 shows the empty state", async () => {
    render(
      <StrictMode>
        <Editor />
      </StrictMode>,
    );
    expect(await screen.findByTestId("memory-working-empty")).toBeInTheDocument();
    expect(gets).toEqual([PATH]);
  });

  it("two editors of the same note (card and Memory tab) share one GET; a remount reuses it", async () => {
    notes.set(PATH, note("shared", 3));
    const first = render(
      <>
        <Editor />
        <Editor />
      </>,
    );
    await waitFor(() => expect(screen.getAllByTestId("memory-working-content")).toHaveLength(2));
    first.unmount();
    render(<Editor />);
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("shared");
    expect(gets).toEqual([PATH]);
  });

  it("a seen write (refreshKey) reads again once, even under StrictMode", async () => {
    const view = render(
      <StrictMode>
        <Editor refreshKey={0} />
      </StrictMode>,
    );
    await screen.findByTestId("memory-working-empty");
    notes.set(PATH, note("written by the agent", 1));
    view.rerender(
      <StrictMode>
        <Editor refreshKey={1} />
      </StrictMode>,
    );
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("written by the agent");
    expect(gets).toEqual([PATH, PATH]);
  });

  it("after Clear, the next editor of the note needs no GET (Clear → New conversation → send)", async () => {
    notes.set(PATH, note("old", 2));
    const view = render(<Editor />);
    fireEvent.click(await screen.findByTestId("memory-working-clear"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear" }));
    await screen.findByTestId("memory-working-empty");
    view.unmount();
    render(<Editor />);
    expect(await screen.findByTestId("memory-working-empty")).toBeInTheDocument();
    expect(gets).toEqual([PATH]);
  });

  it("a conversation Studio just created (primed: no note) needs no GET", async () => {
    primeWorkingMemory(targetKeyOf(target)!, null);
    render(<Editor />);
    expect(await screen.findByTestId("memory-working-empty")).toBeInTheDocument();
    expect(gets).toEqual([]);
  });

  it("Retry after a failed read reads again", async () => {
    let fail = true;
    vi.mocked(fetch).mockImplementation(async (input) => {
      gets.push(new URL(String(input)).pathname);
      if (fail) return json({ detail: "boom" }, 500);
      return json(note("back", 1));
    });
    render(<Editor />);
    await screen.findByTestId("memory-working-error");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("back");
    expect(gets).toEqual([PATH, PATH]);
  });
});

describe("a deleted note is never served from the cache (review blocker)", () => {
  const USER_PATH = "/api/v1/agents/a1/memory/resources/alice/working";
  const userTarget = { scope: "resource" as const, agentId: "a1", resourceId: "alice" };
  const UserEditor = () => <WorkingMemoryEditor target={userTarget} format="markdown" starter="# Notes" />;

  for (const [name, agentId] of [["this agent", "a1"], ["every agent (tenant-wide)", null]] as const) {
    it(`Forget user (${name}): looking the user up again shows no note, from a fresh GET`, async () => {
      notes.set(USER_PATH, { ...note("alice likes tea", 4), scope: "resource", scope_key: "alice" });
      const first = render(<UserEditor />);
      expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("alice likes tea");
      first.unmount();
      await forgetResource("alice", { agentId, includeConversations: true });
      render(<UserEditor />);
      expect(await screen.findByTestId("memory-working-empty")).toBeInTheDocument();
      expect(gets).toEqual([USER_PATH, USER_PATH]);
    });
  }

  it("deleting a conversation: its per-conversation note is not shown again, from a fresh GET", async () => {
    notes.set(PATH, note("conversation note", 2));
    const first = render(<Editor />);
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("conversation note");
    first.unmount();
    await deleteConversation("c1");
    render(<Editor />);
    expect(await screen.findByTestId("memory-working-empty")).toBeInTheDocument();
    expect(gets).toEqual([PATH, PATH]);
  });

  it("a conversation already gone (404) also drops the cached note", async () => {
    notes.set(PATH, note("stale", 2));
    const first = render(<Editor />);
    await screen.findByTestId("memory-working-content");
    first.unmount();
    notes.delete(PATH);
    vi.mocked(fetch).mockImplementationOnce(async () => json({ detail: "conversation not found" }, 404));
    await expect(deleteConversation("c1")).rejects.toBeTruthy();
    render(<Editor />);
    expect(await screen.findByTestId("memory-working-empty")).toBeInTheDocument();
    expect(gets).toEqual([PATH, PATH]);
  });
});

describe("conflictMessage", () => {
  it("says what changed, without If-Match", () => {
    const changed = conflictMessage("working memory is at version 4, not the If-Match version 3; reload it and apply the edit again");
    expect(changed).toBe("The note changed while you were editing it: it is now version 4, and your edit started from version 3. Reload it and apply your edit again.");
    expect(conflictMessage("working memory does not exist (version 0), not the If-Match version 2; reload it and apply the edit again")).toBe(
      "The note was cleared while you were editing it. Reload, then apply your edit again.",
    );
    expect(conflictMessage("working memory is at version 1, not the If-Match version 0; reload it")).toBe(
      "A note was written while you were editing (now version 1). Reload it and apply your edit again.",
    );
    for (const text of [changed, conflictMessage("something else"), conflictMessage("")]) expect(text).not.toMatch(/If-Match/);
  });
});
