import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryTab } from "./MemoryTab";

const nav = vi.hoisted(() => ({ search: "", push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/agents/a1/memory",
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
  useParams: () => ({ id: "a1" }),
}));

const T0 = "2026-11-14T10:00:00Z";
let memory: unknown = { working: { scope: "resource", template: "# User" } };
let calls: { path: string; method: string; query: URLSearchParams }[];

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function respond(path: string, method: string): Response {
  const spec = { spec_version: 1, name: "Support", instructions: "", model: { provider: "test", name: "echo" }, tools: [], ...(memory ? { memory } : {}) };
  if (path === "/agents/a1") return json({ id: "a1", name: "Support", latest_version: 1, draft_spec: spec });
  if (path === "/agents/a1/versions") return json([{ agent_id: "a1", version: 1, spec, spec_hash: "h", created_at: T0, created_by: "dev" }]);
  if (path === "/agents/a1/memory/working") return json({ items: [{ agent_id: "a1", scope: "resource", scope_key: "alice", format: "markdown", content: "# Alice", version: 1, updated_by_run_id: null, created_at: T0, updated_at: T0 }] });
  if (path === "/conversations") return json({ items: [{ id: "c1", agent_id: "a1", resource_id: "bob", title: "x", turn_count: 1, created_at: T0, updated_at: T0 }], next_before: null });
  if (path === "/agents/a1/memory/resources/alice/working") return json({ agent_id: "a1", scope: "resource", scope_key: "alice", format: "markdown", content: "# Alice", version: 1, updated_by_run_id: null, created_at: T0, updated_at: T0 });
  if (path === "/agents/a1/memory/resources/alice" && method === "DELETE") {
    return json({ resource_id: "alice", agent_id: "a1", working_memories_deleted: 1, message_embeddings_deleted: 0, conversations_deleted: [], busy_conversations: [] });
  }
  return json({ detail: "Not found" }, 404);
}

beforeEach(() => {
  calls = [];
  nav.search = "";
  nav.push.mockClear();
  nav.replace.mockClear();
  memory = { working: { scope: "resource", template: "# User" } };
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      const path = url.pathname.replace(/^\/api\/v1/, "");
      calls.push({ path, method: init.method ?? "GET", query: url.searchParams });
      return respond(path, init.method ?? "GET");
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Memory tab", () => {
  it("an agent without memory is sent to Chat", async () => {
    memory = null;
    render(<MemoryTab agentId="a1" />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/agents/a1/chat"));
    expect(screen.getByTestId("memory-tab-no-memory")).toBeInTheDocument();
  });

  it("lists users from working memory notes and conversations; a pick or an entered id opens it", async () => {
    render(<MemoryTab agentId="a1" />);
    const options = await screen.findAllByTestId("memory-resource-option");
    expect(options.map((o) => o.textContent)).toEqual(["aliceWorking memory", "bobConversations"]);
    fireEvent.click(options[1]);
    expect(nav.push).toHaveBeenCalledWith("/agents/a1/memory?resource=bob");
    fireEvent.change(screen.getByTestId("memory-resource-input"), { target: { value: "no spaces allowed" } });
    fireEvent.click(screen.getByTestId("memory-resource-open"));
    expect(screen.getByTestId("memory-resource-error")).toHaveTextContent("1 to 128 characters");
    fireEvent.change(screen.getByTestId("memory-resource-input"), { target: { value: "carol@example.com" } });
    fireEvent.click(screen.getByTestId("memory-resource-open"));
    expect(nav.push).toHaveBeenLastCalledWith("/agents/a1/memory?resource=carol%40example.com");
  });

  it("one user: their working memory, and Forget user with include_conversations=true by default", async () => {
    nav.search = "resource=alice";
    render(<MemoryTab agentId="a1" />);
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("# Alice");
    expect(screen.getByTestId("memory-user-title")).toHaveTextContent("alice");
    expect(screen.getByTestId("memory-working-edit")).toBeEnabled();
    fireEvent.click(screen.getByTestId("memory-forget-user"));
    const dialog = await screen.findByRole("dialog", { name: "Forget alice?" });
    expect(dialog).toHaveTextContent("conversations with Support and their transcripts");
    fireEvent.click(within(dialog).getByTestId("memory-forget-confirm"));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/agents/a1/memory"));
    // Focus goes to the user id field, never <body>.
    await waitFor(() => expect(screen.getByTestId("memory-resource-input")).toHaveFocus());
    const forget = calls.find((c) => c.method === "DELETE")!;
    expect(forget.path).toBe("/agents/a1/memory/resources/alice");
    expect(forget.query.get("include_conversations")).toBe("true");
  });

  it("per-conversation working memory: per-user edits are refused, with the reason", async () => {
    memory = { working: { scope: "conversation" } };
    nav.search = "resource=alice";
    render(<MemoryTab agentId="a1" />);
    expect(await screen.findByTestId("memory-per-conversation")).toBeInTheDocument();
    expect(await screen.findByTestId("memory-working-edit")).toBeDisabled();
    expect(screen.getByTestId("memory-working-blocked")).toHaveTextContent("per conversation");
  });
});
