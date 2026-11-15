import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkingMemoryEditor } from "./WorkingMemoryEditor";
import { ObservationsList } from "./ObservationsList";
import { ForgetUserDialog, forgetSummary } from "./ForgetUserDialog";
import { recallText } from "./MemoryInspector";
import { useMemoryActivity, type LatestTurn } from "./useMemoryActivity";
import { ACT_AS_PREFIX, MEMORY_CARD_KEY, agentHasMemory, editBlockedReason, readActAs, workingFormat, workingStarter, writeActAs } from "./memoryModel";
import { ComposerActAsChip, actAsLabel } from "@/components/agentPage/ComposerActAsChip";
import { MemoryCard } from "@/components/agentPage/MemoryCard";
import { visibleTabs } from "@/components/agentPage/AgentLayout";
import { agentTabOf, routes } from "@/lib/routes";
import type { Agent, AgentSpec } from "@/lib/api";

vi.mock("next/navigation", () => ({
  usePathname: () => "/agents/a1/chat",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({ id: "a1" }),
}));

type Call = { url: URL; method: string; headers: Headers; body: unknown; signal?: AbortSignal | null };
let calls: Call[];
let answer: (url: URL, method: string, body: unknown) => Response;

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  calls = [];
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = new URL(input);
      const method = init.method ?? "GET";
      const body = init.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ url, method, headers: new Headers(init.headers), body, signal: init.signal });
      return answer(url, method, body);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

const T0 = "2026-11-14T10:00:00Z";
const note = (content: string | Record<string, unknown>, version = 2, format: "markdown" | "json" = typeof content === "string" ? "markdown" : "json") => ({
  agent_id: "a1",
  scope: "conversation",
  scope_key: "c1",
  format,
  content,
  version,
  updated_by_run_id: "r1",
  created_at: T0,
  updated_at: T0,
});

const target = { scope: "conversation" as const, conversationId: "c1" };

describe("WorkingMemoryEditor", () => {
  it("shows the note, saves an edit with If-Match on its version", async () => {
    answer = (_url, method, body) => (method === "PUT" ? json(note((body as { content: string }).content, 3)) : json(note("# User\n- Name: Ada")));
    render(<WorkingMemoryEditor target={target} format="markdown" starter="# User" />);
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("Name: Ada");
    fireEvent.click(screen.getByTestId("memory-working-edit"));
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "# User\n- Name: Ada Lovelace" } });
    fireEvent.click(screen.getByTestId("memory-working-save"));
    await waitFor(() => expect(screen.getByTestId("memory-working-content")).toHaveTextContent("Ada Lovelace"));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.headers.get("If-Match")).toBe('"2"');
    expect(put.body).toEqual({ content: "# User\n- Name: Ada Lovelace" });
  });

  it("a 409 shows the conflict and Reload loads the newer note into the editor", async () => {
    let version = 2;
    answer = (_url, method) =>
      method === "PUT"
        ? json({ detail: "working memory is at version 5, not the If-Match version 2; reload it and apply the edit again" }, 409)
        : json(note(version === 2 ? "old" : "newer", version));
    render(<WorkingMemoryEditor target={target} format="markdown" starter="" />);
    await screen.findByTestId("memory-working-content");
    fireEvent.click(screen.getByTestId("memory-working-edit"));
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "mine" } });
    version = 5;
    fireEvent.click(screen.getByTestId("memory-working-save"));
    expect(await screen.findByTestId("memory-working-conflict")).toHaveTextContent("version 5");
    expect(screen.getByTestId("memory-working-save")).toBeDisabled();
    fireEvent.click(screen.getByTestId("memory-working-reload"));
    await waitFor(() => expect(screen.getByTestId("memory-working-textarea")).toHaveValue("newer"));
    expect(screen.queryByTestId("memory-working-conflict")).toBeNull();
  });

  it("JSON schema mode: refuses JSON that is not an object before sending", async () => {
    answer = () => json(note({ name: "Ada" }, 1));
    render(<WorkingMemoryEditor target={target} format="json" starter="{}" />);
    await screen.findByTestId("memory-working-content");
    fireEvent.click(screen.getByTestId("memory-working-edit"));
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue('{\n  "name": "Ada"\n}');
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "[1, 2]" } });
    fireEvent.click(screen.getByTestId("memory-working-save"));
    expect(await screen.findByTestId("memory-working-save-error")).toHaveTextContent("JSON object");
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("no note: Create starts from the template and sends If-Match 0", async () => {
    answer = (_url, method, body) => (method === "PUT" ? json(note((body as { content: string }).content, 1)) : json({ detail: "Working memory not found" }, 404));
    render(<WorkingMemoryEditor target={target} format="markdown" starter={"# User\n- Name:"} />);
    fireEvent.click(await screen.findByTestId("memory-working-create"));
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue("# User\n- Name:");
    fireEvent.click(screen.getByTestId("memory-working-save"));
    await screen.findByTestId("memory-working-content");
    expect(calls.find((c) => c.method === "PUT")!.headers.get("If-Match")).toBe('"0"');
  });

  it("Clear confirms, then deletes with If-Match", async () => {
    answer = (_url, method) => (method === "DELETE" ? json(null, 204) : json(note("x", 7)));
    render(<WorkingMemoryEditor target={target} format="markdown" starter="" />);
    fireEvent.click(await screen.findByTestId("memory-working-clear"));
    const dialog = await screen.findByRole("dialog", { name: "Clear working memory?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Clear" }));
    await screen.findByTestId("memory-working-empty");
    expect(calls.find((c) => c.method === "DELETE")!.headers.get("If-Match")).toBe('"7"');
  });

  it("an edit the API would refuse is disabled, with the reason", async () => {
    answer = () => json(note("x"));
    render(<WorkingMemoryEditor target={target} format="markdown" starter="" editBlocked="Publish a version with working memory to edit it." />);
    expect(await screen.findByTestId("memory-working-edit")).toBeDisabled();
    expect(screen.getByTestId("memory-working-blocked")).toHaveTextContent("Publish a version");
  });

  it("after a template → schema switch, a Markdown note is read-only with the reason; Clear still works, no PUT is sent", async () => {
    answer = (_url, method) => (method === "DELETE" ? json(null, 204) : json(note("# User\n- Name: Ada", 4, "markdown")));
    render(<WorkingMemoryEditor target={target} format="json" starter="{}" />);
    expect(await screen.findByTestId("memory-working-content")).toHaveTextContent("Name: Ada");
    expect(screen.getByTestId("memory-working-edit")).toBeDisabled();
    expect(screen.getByTestId("memory-working-format-mismatch")).toHaveTextContent(
      "stored as Markdown (template), but the latest published version's working memory is JSON (schema)",
    );
    expect(screen.queryByTestId("memory-working-textarea")).toBeNull();
    fireEvent.click(screen.getByTestId("memory-working-clear"));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Clear working memory?" })).getByRole("button", { name: "Clear" }));
    await screen.findByTestId("memory-working-empty");
    expect(screen.queryByTestId("memory-working-format-mismatch")).toBeNull();
    // Create now starts in the published format.
    fireEvent.click(screen.getByTestId("memory-working-create"));
    expect(screen.getByTestId("memory-working-textarea")).toHaveValue("{}");
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("after a schema → template switch, a JSON note is read-only too", async () => {
    answer = () => json(note({ name: "Ada" }, 2, "json"));
    render(<WorkingMemoryEditor target={target} format="markdown" starter="# User" />);
    expect(await screen.findByTestId("memory-working-format-mismatch")).toHaveTextContent("stored as JSON (schema)");
    expect(screen.getByTestId("memory-working-edit")).toBeDisabled();
  });

  it("a lifted draft of a note in the other format is not offered for editing", async () => {
    answer = () => json(note("old text", 2, "markdown"));
    const key = JSON.stringify(target);
    const store = { draft: { key, text: "edited", original: "old text", baseVersion: 2 }, setDraft: vi.fn() };
    render(<WorkingMemoryEditor target={target} format="json" starter="{}" draftStore={store} />);
    await screen.findByTestId("memory-working-format-mismatch");
    expect(screen.queryByTestId("memory-working-textarea")).toBeNull();
    expect(screen.getByTestId("memory-working-content")).toHaveTextContent("old text");
  });

  it("reports dirty while the edit differs", async () => {
    answer = () => json(note("x"));
    const onDirty = vi.fn();
    render(<WorkingMemoryEditor target={target} format="markdown" starter="" onDirtyChange={onDirty} />);
    fireEvent.click(await screen.findByTestId("memory-working-edit"));
    expect(onDirty).toHaveBeenLastCalledWith(false);
    fireEvent.change(screen.getByTestId("memory-working-textarea"), { target: { value: "y" } });
    expect(onDirty).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByTestId("memory-working-cancel"));
    expect(onDirty).toHaveBeenLastCalledWith(false);
  });
});

describe("ObservationsList", () => {
  const row = (id: string, from: number, to: number, extra: Record<string, unknown> = {}) => ({
    id, agent_id: "a1", conversation_id: "c1", kind: "observation", covers_from_turn: from, covers_to_turn: to,
    content: `Notes ${id}`, approx_tokens: 50, run_id: "r1", superseded_by: null, created_at: T0, expired: false, ...extra,
  });

  it("renders an expired marker as Expired (never an empty note) and only the newest has Delete", async () => {
    answer = () => json({ items: [row("o1", 1, 4), row("m1", 5, 8, { expired: true, content: "", approx_tokens: 0 })], covered_to_turn: 8 });
    render(<ObservationsList conversationId="c1" />);
    const rows = await screen.findAllByTestId("memory-observation-row");
    expect(rows).toHaveLength(2);
    expect(rows[1]).toHaveAttribute("data-expired", "true");
    expect(within(rows[1]).getByTestId("memory-observation-expired")).toHaveTextContent("Expired");
    expect(rows[1]).toHaveTextContent("text aged out");
    expect(within(rows[0]).queryByTestId("memory-observation-delete")).toBeNull();
    expect(within(rows[0]).getByTestId("memory-observation-blocked")).toHaveTextContent("expired");
    expect(within(rows[1]).getByTestId("memory-observation-delete")).toBeInTheDocument();
    expect(screen.getByTestId("memory-observations-covered")).toHaveTextContent("Turns 1–8");
  });

  it("deleting the newest confirms, sends DELETE for it and reloads; a 409 shows its detail", async () => {
    let items = [row("o1", 1, 4), row("o2", 5, 9)];
    answer = (url, method) => {
      if (method === "DELETE") {
        items = items.filter((item) => !url.pathname.endsWith(item.id));
        return json(null, 204);
      }
      return json({ items, covered_to_turn: 9 });
    };
    render(<ObservationsList conversationId="c1" />);
    await screen.findAllByTestId("memory-observation-row");
    fireEvent.click(screen.getByTestId("memory-observation-delete"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.getAllByTestId("memory-observation-row")).toHaveLength(1));
    expect(calls.find((c) => c.method === "DELETE")!.url.pathname).toBe("/api/v1/conversations/c1/memory/observations/o2");

    answer = (_url, method) =>
      method === "DELETE"
        ? json({ detail: "only the newest active observation (expired ones included) can be deleted" }, 409)
        : json({ items, covered_to_turn: 4 });
    fireEvent.click(screen.getByTestId("memory-observation-delete"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }));
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("expired ones included");
  });

  it("Delete all", async () => {
    answer = (_url, method) => (method === "DELETE" ? json({ deleted: 2 }) : json({ items: method === "GET" && calls.some((c) => c.method === "DELETE") ? [] : [row("o1", 1, 4)], covered_to_turn: 0 }));
    render(<ObservationsList conversationId="c1" />);
    fireEvent.click(await screen.findByTestId("memory-observations-delete-all"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete all" }));
    await screen.findByTestId("memory-observations-empty");
    expect(calls.find((c) => c.method === "DELETE")!.url.pathname).toBe("/api/v1/conversations/c1/memory/observations");
  });
});

describe("ForgetUserDialog", () => {
  const result = { resource_id: "alice", agent_id: "a1", working_memories_deleted: 1, message_embeddings_deleted: 4, conversations_deleted: ["c1", "c2"], busy_conversations: [] };

  it("defaults to this agent with include_conversations=true, and says the conversations go too", async () => {
    answer = () => json(result);
    const onDone = vi.fn();
    render(<ForgetUserDialog agentId="a1" agentName="Support" resourceId="alice" onDone={onDone} onCancel={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Forget alice?" });
    expect(dialog).toHaveTextContent("also deletes this user's conversations with Support and their transcripts");
    expect(screen.getByTestId("memory-forget-scope-agent")).toBeChecked();
    expect(screen.getByTestId("memory-forget-keep-conversations")).not.toBeChecked();
    fireEvent.click(screen.getByTestId("memory-forget-confirm"));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(result));
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url.pathname).toBe("/api/v1/agents/a1/memory/resources/alice");
    expect(calls[0].url.searchParams.get("include_conversations")).toBe("true");
  });

  it("all agents, keeping the conversations", async () => {
    answer = () => json({ ...result, agent_id: null, conversations_deleted: [] });
    render(<ForgetUserDialog agentId="a1" agentName="Support" resourceId="alice" onDone={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByTestId("memory-forget-scope-all"));
    fireEvent.click(screen.getByTestId("memory-forget-keep-conversations"));
    expect(screen.getByRole("dialog")).not.toHaveTextContent("also deletes this user's conversations");
    fireEvent.click(screen.getByTestId("memory-forget-confirm"));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url.pathname).toBe("/api/v1/memory/resources/alice");
    expect(calls[0].url.searchParams.get("include_conversations")).toBe("false");
  });

  it("a busy 409 deletes nothing and says so", async () => {
    answer = () => json({ detail: "a turn is still queued or running in 1 conversation(s) of this resource; nothing was deleted. Retry when the turns finish", busy_conversations: ["c1"] }, 409);
    const onDone = vi.fn();
    render(<ForgetUserDialog agentId="a1" agentName="Support" resourceId="alice" onDone={onDone} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByTestId("memory-forget-confirm"));
    expect(await screen.findByTestId("memory-forget-error")).toHaveTextContent("nothing was deleted");
    expect(onDone).not.toHaveBeenCalled();
  });

  it("summarises what was removed", () => {
    expect(forgetSummary(result)).toBe("Forgot alice: 1 working memory, 4 recall entries, 2 conversations deleted");
  });
});

describe("Act as user chip", () => {
  it("validates the id as the API does and applies it", async () => {
    const onChange = vi.fn();
    render(<ComposerActAsChip value="" onChange={onChange} />);
    const chip = screen.getByTestId("composer-chip-act-as");
    expect(chip).toHaveAccessibleName("Act as user: You");
    fireEvent.click(chip);
    const input = await screen.findByTestId("act-as-input");
    fireEvent.change(input, { target: { value: "not valid!" } });
    expect(screen.getByTestId("act-as-error")).toHaveTextContent("1 to 128 characters");
    expect(screen.getByTestId("act-as-apply")).toBeDisabled();
    fireEvent.change(input, { target: { value: ".." } });
    expect(screen.getByTestId("act-as-error")).toHaveTextContent("dots only");
    fireEvent.change(input, { target: { value: "  alice@example.com " } });
    fireEvent.click(screen.getByTestId("act-as-apply"));
    expect(onChange).toHaveBeenCalledWith("alice@example.com");
  });

  it("an open conversation shows its own user: the choice waits for a new conversation", async () => {
    render(<ComposerActAsChip value="bob" onChange={vi.fn()} conversationUser="alice" />);
    expect(screen.getByTestId("composer-chip-act-as")).toHaveAccessibleName("Act as user: alice (next: bob)");
    fireEvent.click(screen.getByTestId("composer-chip-act-as"));
    expect(await screen.findByTestId("act-as-fixed")).toHaveTextContent("next new conversation");
    expect(actAsLabel("", undefined)).toBe("You");
    expect(actAsLabel("bob", null)).toBe("No user (next: bob)");
    expect(actAsLabel("alice", "alice")).toBe("alice");
    expect(actAsLabel("", "alice")).toBe("alice");
  });

  it("remembers the id per agent in this tab's sessionStorage", () => {
    writeActAs("a1", "alice");
    expect(readActAs("a1")).toBe("alice");
    expect(readActAs("a2")).toBe("");
    expect(Object.keys(window.sessionStorage).every((key) => key.startsWith(ACT_AS_PREFIX))).toBe(true);
    writeActAs("a1", "");
    expect(readActAs("a1")).toBe("");
  });
});

describe("MemoryCard", () => {
  const spec = { spec_version: 1, name: "x", instructions: "", model: { provider: "test", name: "echo" }, tools: [], memory: { working: { scope: "conversation" }, observational: { model: { provider: "test", name: "observer" } } } } as unknown as AgentSpec;
  const activity = { recall: { status: "none" as const }, revision: 0, updating: false };

  it("expands into the inspector and remembers it", async () => {
    answer = (url) => (url.pathname.endsWith("/observations") ? json({ items: [], covered_to_turn: 0 }) : json({ detail: "Working memory not found" }, 404));
    render(<MemoryCard spec={spec} inspector={{ agentId: "a1", conversationId: "c1", resourceId: "alice", publishedSpec: spec, activity }} />);
    const toggle = screen.getByTestId("memory-card-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(window.localStorage.getItem(MEMORY_CARD_KEY)).toBe("open");
    expect(await screen.findByTestId("memory-working-empty")).toBeInTheDocument();
    expect(await screen.findByTestId("memory-observations-empty")).toBeInTheDocument();
    expect(calls.map((c) => c.url.pathname)).toEqual(expect.arrayContaining(["/api/v1/conversations/c1/memory/working", "/api/v1/conversations/c1/memory/observations"]));
  });

  it("shows Memory updating while post-turn steps run", () => {
    window.localStorage.setItem(MEMORY_CARD_KEY, "open");
    answer = (url) => (url.pathname.endsWith("/observations") ? json({ items: [], covered_to_turn: 0 }) : json({ detail: "Working memory not found" }, 404));
    const props = { agentId: "a1", conversationId: "c1", resourceId: "alice", publishedSpec: spec, activity: { ...activity, updating: true } };
    const view = render(<MemoryCard spec={spec} inspector={props} />);
    expect(screen.getByTestId("memory-updating")).toHaveTextContent("Memory updating");
    // Collapsed, the card says it in its header.
    fireEvent.click(screen.getByTestId("memory-card-toggle"));
    expect(screen.getByTestId("memory-card-updating")).toHaveTextContent("Updating");
    view.unmount();
    render(<MemoryCard spec={spec} inspector={{ ...props, conversationId: null }} />);
    fireEvent.click(screen.getByTestId("memory-card-toggle"));
    expect(screen.getByTestId("memory-inspector-empty")).toBeInTheDocument();
  });

  it("an agent without memory has no toggle", () => {
    const plain = { ...spec, memory: undefined } as unknown as AgentSpec;
    render(<MemoryCard spec={plain} inspector={{ agentId: "a1", conversationId: "c1", resourceId: null, publishedSpec: null, activity }} />);
    expect(screen.queryByTestId("memory-card-toggle")).toBeNull();
  });
});

describe("useMemoryActivity", () => {
  const turn = (status: string, events: LatestTurn["events"] = []): LatestTurn => ({ runId: "r1", status, events });

  it("after a watched turn completes, reads its events until the observer reports", async () => {
    vi.useFakeTimers();
    let served: unknown[] = [];
    answer = () => json(served);
    const features = { observational: true, semanticRecall: false };
    const { result, rerender } = renderHook(({ t }) => useMemoryActivity(t, features, { pollMs: 100, timeoutMs: 5000 }), { initialProps: { t: turn("running") } });
    expect(result.current.updating).toBe(false);
    rerender({ t: turn("completed", [{ seq: 3, type: "run_completed", data: {} }]) });
    expect(result.current.updating).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(calls[0].url.pathname).toBe("/api/v1/runs/r1/events");
    expect(calls[0].url.searchParams.get("after_seq")).toBe("3");
    expect(result.current.updating).toBe(true);
    served = [{ seq: 4, type: "memory_updated", data: { stage: "observe", notes: 2 } }];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(result.current.updating).toBe(false);
    expect(result.current.revision).toBe(1);
  });

  it("stops as soon as post_turn_done arrives, on a quiet turn and while a reflection is due (P1.C1)", async () => {
    vi.useFakeTimers();
    let served: unknown[] = [];
    answer = (url) =>
      url.pathname.endsWith("/memory/observations")
        ? json({ items: [obsRow("o1", 5000), obsRow("o2", 5000)], covered_to_turn: 4 })
        : json(served);
    const features = { observational: true, semanticRecall: true, reflectAfterTokens: 8000 };
    const { result, rerender } = renderHook(({ t }) => useMemoryActivity(t, features, { pollMs: 100, timeoutMs: 20_000, conversationId: "c1" }), {
      initialProps: { t: turn("running") },
    });
    rerender({ t: turn("completed", [{ seq: 3, type: "run_completed", data: {} }]) });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(result.current.updating).toBe(true);
    // The observe leaves a reflection due (10000 > 8000), so without the marker the poll would go on.
    served = [{ seq: 4, type: "memory_updated", data: { stage: "observe" } }];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(result.current.updating).toBe(true);
    served = [{ seq: 5, type: "post_turn_done", data: { observe: "created", index: "done" } }];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(result.current.updating).toBe(false);
    const reads = calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(calls.length).toBe(reads); // well before the 20 s timeout, nothing more is read
  });

  it("a quiet turn ends at post_turn_done instead of the timeout", async () => {
    vi.useFakeTimers();
    answer = () => json([{ seq: 4, type: "post_turn_done", data: { observe: "skipped", index: "off" } }]);
    const features = { observational: true, semanticRecall: false };
    const { result, rerender } = renderHook(({ t }) => useMemoryActivity(t, features, { pollMs: 100, timeoutMs: 20_000 }), { initialProps: { t: turn("running") } });
    rerender({ t: turn("completed") });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(result.current.updating).toBe(false);
    expect(calls).toHaveLength(1);
  });

  const obsRow = (id: string, tokens: number, extra: Record<string, unknown> = {}) => ({
    id, agent_id: "a1", conversation_id: "c1", kind: "observation", covers_from_turn: 1, covers_to_turn: 2,
    content: "note", approx_tokens: tokens, run_id: "r1", superseded_by: null, created_at: "2026-11-14T00:00:00Z", ...extra,
  });

  it("after an observe that leaves a reflection due, keeps polling until the reflect event", async () => {
    vi.useFakeTimers();
    let served: unknown[] = [];
    answer = (url) => (url.pathname.endsWith("/memory/observations") ? json({ items: [obsRow("o1", 5000), obsRow("o2", 4000)], covered_to_turn: 4 }) : json(served));
    const features = { observational: true, semanticRecall: false, reflectAfterTokens: 8000 };
    const { result, rerender } = renderHook(({ t }) => useMemoryActivity(t, features, { pollMs: 100, timeoutMs: 5000, conversationId: "c1" }), {
      initialProps: { t: turn("running") },
    });
    rerender({ t: turn("completed", [{ seq: 3, type: "run_completed", data: {} }]) });
    served = [{ seq: 4, type: "memory_updated", data: { stage: "observe", notes: 2 } }];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(result.current.revision).toBe(1);
    // 9000 tokens over two rows > 8000: the reflector runs next, so the poll goes on.
    expect(calls.filter((c) => c.url.pathname === "/api/v1/conversations/c1/memory/observations")).toHaveLength(1);
    expect(result.current.updating).toBe(true);
    served = [{ seq: 5, type: "memory_updated", data: { stage: "reflect", notes: 1 } }];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(result.current.updating).toBe(false);
    expect(result.current.revision).toBe(2);
    // The observations are read once per poll, not at every tick.
    expect(calls.filter((c) => c.url.pathname.endsWith("/memory/observations"))).toHaveLength(1);
  });

  it("stops at the observe when no reflection can follow it (under the threshold, or one row)", async () => {
    for (const items of [[obsRow("o1", 3000), obsRow("o2", 4000)], [obsRow("o1", 9000)], [obsRow("o1", 6000), obsRow("o2", 6000, { expired: true, content: "" })]]) {
      vi.useFakeTimers();
      calls = [];
      answer = (url) => (url.pathname.endsWith("/memory/observations") ? json({ items, covered_to_turn: 4 }) : json([{ seq: 4, type: "memory_updated", data: { stage: "observe" } }]));
      const features = { observational: true, semanticRecall: false, reflectAfterTokens: 8000 };
      const hook = renderHook(({ t }) => useMemoryActivity(t, features, { pollMs: 100, timeoutMs: 5000, conversationId: "c1" }), { initialProps: { t: turn("running") } });
      hook.rerender({ t: turn("completed") });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(150);
      });
      expect(hook.result.current.updating).toBe(false);
      hook.unmount();
      vi.useRealTimers();
    }
  });

  it("waiting for a reflection that never comes ends at the same deadline; a failed observe never waits", async () => {
    vi.useFakeTimers();
    answer = (url) => (url.pathname.endsWith("/memory/observations") ? json({ items: [obsRow("o1", 5000), obsRow("o2", 5000)], covered_to_turn: 4 }) : json([{ seq: 4, type: "memory_updated", data: { stage: "observe" } }]));
    const features = { observational: true, semanticRecall: false, reflectAfterTokens: 8000 };
    const { result, rerender } = renderHook(({ t }) => useMemoryActivity(t, features, { pollMs: 100, timeoutMs: 450, conversationId: "c1" }), { initialProps: { t: turn("running") } });
    rerender({ t: turn("completed") });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(result.current.updating).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(result.current.updating).toBe(false);
    const reads = calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(calls.length).toBe(reads);
  });

  it("stops at the timeout when the observer had nothing to do; an old completed turn is never polled", async () => {
    vi.useFakeTimers();
    answer = () => json([]);
    const features = { observational: true, semanticRecall: false };
    const { result, rerender } = renderHook(({ t }) => useMemoryActivity(t, features, { pollMs: 100, timeoutMs: 300 }), { initialProps: { t: turn("running") } });
    rerender({ t: turn("completed") });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.updating).toBe(false);
    const polls = calls.length;
    expect(polls).toBeGreaterThan(0);

    const old = renderHook(() => useMemoryActivity(turn("completed"), features, { pollMs: 100, timeoutMs: 300 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(old.result.current.updating).toBe(false);
    expect(calls.length).toBe(polls);
  });

  it("a feature toggled mid-poll does not restart the deadline, and nothing is read after it", async () => {
    vi.useFakeTimers();
    answer = () => json([]);
    const { rerender, result } = renderHook(({ t, f }) => useMemoryActivity(t, f, { pollMs: 100, timeoutMs: 450 }), {
      initialProps: { t: turn("running"), f: { observational: true, semanticRecall: true } },
    });
    rerender({ t: turn("completed"), f: { observational: true, semanticRecall: true } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    // Toggled at 250 ms: the poll keeps its 450 ms deadline.
    rerender({ t: turn("completed"), f: { observational: true, semanticRecall: false } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(result.current.updating).toBe(false);
    const reads = calls.length;
    expect(reads).toBeLessThanOrEqual(5);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(calls.length).toBe(reads);
  });

  it("unmounting abandons the read in flight: its answer is never applied", async () => {
    vi.useFakeTimers();
    let release: (response: Response) => void = () => {};
    answer = () => new Promise<Response>((resolve) => { release = resolve; }) as unknown as Response;
    const { rerender, unmount } = renderHook(({ t }) => useMemoryActivity(t, { observational: true, semanticRecall: false }, { pollMs: 100, timeoutMs: 5000 }), {
      initialProps: { t: turn("running") },
    });
    rerender({ t: turn("completed") });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(calls).toHaveLength(1);
    const errors = vi.spyOn(console, "error");
    const signal = calls[0].signal;
    expect(signal?.aborted).toBe(false);
    unmount();
    // The request itself is cancelled.
    expect(signal?.aborted).toBe(true);
    release(json([{ seq: 9, type: "memory_updated", data: { stage: "observe" } }]));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(calls).toHaveLength(1);
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("summarises recall from the turn's events", () => {
    const { result } = renderHook(() => useMemoryActivity(turn("completed", [{ seq: 1, type: "embedding", data: { stage: "recall", hits: 3, turns: 2, groups: 1, messages: 4 } }]), { observational: false, semanticRecall: true }));
    expect(result.current.recall).toEqual({ status: "done", hits: 3, turns: 2, groups: 1, messages: 4 });
    expect(recallText(result.current.recall)).toBe("The last turn recalled 2 earlier turns (3 hits); 4 messages in 1 group reached the model.");
  });
});

describe("memory rules and routes", () => {
  const agent = (memory: unknown, latest: number | null = null) => ({ id: "a1", name: "A", latest_version: latest, draft_spec: { memory } }) as unknown as Agent;

  it("an agent has memory when its draft or latest version has it", () => {
    expect(agentHasMemory(agent(undefined), [])).toBe(false);
    expect(agentHasMemory(agent({}), [])).toBe(true);
    const versions = [{ agent_id: "a1", version: 1, spec: { memory: { working: {} } } }] as never;
    expect(agentHasMemory(agent(undefined, 1), versions)).toBe(true);
  });

  it("edits are checked against the latest published working memory and its scope", () => {
    const spec = (working: unknown) => ({ memory: { working } }) as unknown as AgentSpec;
    expect(editBlockedReason(null, "resource")).toMatch(/Publish a version/);
    expect(editBlockedReason(spec({ scope: "resource" }), "resource")).toBeNull();
    expect(editBlockedReason(spec({}), "resource")).toBeNull();
    expect(editBlockedReason(spec({ scope: "conversation" }), "resource")).toMatch(/per conversation/);
    expect(editBlockedReason(spec({}), "conversation")).toMatch(/per user/);
    expect(workingFormat({ json_schema: { type: "object" } })).toBe("json");
    expect(workingStarter({ json_schema: { type: "object" } })).toBe("{}");
    expect(workingStarter({ template: "# T" })).toBe("# T");
  });

  it("the Memory tab route, its tab and the header list", () => {
    expect(routes.agentMemory("a1")).toBe("/agents/a1/memory");
    expect(routes.agentMemory("a1", { resource: "alice@example.com" })).toBe("/agents/a1/memory?resource=alice%40example.com");
    expect(routes.agentTab("a1", "memory")).toBe("/agents/a1/memory");
    expect(agentTabOf("/agents/a1/memory")).toBe("memory");
    expect(visibleTabs(false, "chat").map((t) => t.id)).toEqual(["chat", "traces", "editor", "tests"]);
    expect(visibleTabs(true, "chat").map((t) => t.id)).toEqual(["chat", "traces", "editor", "memory", "tests"]);
    expect(visibleTabs(false, "memory").map((t) => t.id)).toContain("memory");
  });
});
