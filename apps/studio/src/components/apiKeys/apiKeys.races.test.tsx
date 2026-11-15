/** AGUI.13 review fixes: stale list answers never win, and the picker can retry a failed load. */
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiKeysSection } from "./ApiKeysSection";
import type { ApiKey } from "@/lib/apiKeys";

const mk = (o: Partial<ApiKey> = {}): ApiKey => ({
  id: "k1", name: "Key one", prefix: "pos_k1_", status: "active", scopes: ["agui:run"], agent_ids: null,
  workflow_ids: null, allowed_origins: null, created_by: "u1", created_at: "2026-11-15T10:00:00Z",
  expires_at: null, revoked_at: null, last_used_at: null, ...o,
});
const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

type Handler = (url: string, init: RequestInit) => Promise<Response> | Response;
let handler: Handler;

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit = {}) => handler(String(input), init)));
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

const isList = (url: string, init: RequestInit) => url.endsWith("/api/v1/api-keys") && (init.method ?? "GET") === "GET";

describe("stale list answers", () => {
  it("a slow first load that resolves after a create does not hide the new key", async () => {
    const first = deferred<Response>();
    let lists = 0;
    let created = false;
    handler = (url, init) => {
      if (isList(url, init)) {
        lists += 1;
        // The first GET is slow and predates the key; later GETs include it.
        return lists === 1 ? first.promise : json({ items: created ? [mk({ id: "new", name: "New key" })] : [] });
      }
      if (url.endsWith("/api/v1/api-keys") && init.method === "POST") {
        created = true;
        return json({ ...mk({ id: "new", name: "New key" }), key: "pos_new_secret" }, 201);
      }
      return json({ detail: "nope" }, 404);
    };
    render(<ApiKeysSection />);
    fireEvent.click(screen.getByTestId("api-key-create"));
    const dialog = await screen.findByTestId("api-key-create-dialog");
    fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: "New key" } });
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
    await screen.findByTestId("api-key-secret");
    fireEvent.click(screen.getByTestId("api-key-done"));
    await screen.findByText("New key");
    // Now the stale first answer arrives, without the key.
    await act(async () => first.resolve(json({ items: [] })));
    expect(screen.getByText("New key")).toBeInTheDocument();
    expect(screen.getAllByTestId("api-key-row")).toHaveLength(1);
  });

  it("revoking A then B: the reload after A resolving last does not show B active again", async () => {
    let keys = [mk({ id: "a", name: "Key A" }), mk({ id: "b", name: "Key B" })];
    const slow = deferred<Response>();
    let lists = 0;
    handler = (url, init) => {
      if (isList(url, init)) {
        lists += 1;
        const snapshot = json({ items: keys });
        // List 2 is the reload after revoking A: hold it until after B's reload.
        if (lists === 2) return slow.promise.then(() => snapshot);
        return snapshot;
      }
      const revoke = /\/api-keys\/([^/]+)$/.exec(url);
      if (revoke && init.method === "DELETE") {
        keys = keys.map((k) => (k.id === revoke[1] ? { ...k, status: "revoked" as const, revoked_at: "2026-11-15T11:00:00Z" } : k));
        return new Response(null, { status: 204 });
      }
      return json({ detail: "nope" }, 404);
    };
    render(<ApiKeysSection />);
    await screen.findByText("Key B");
    const revokeOf = (name: string) => screen.getByRole("button", { name: `Revoke ${name}` });

    fireEvent.click(revokeOf("Key A"));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.queryByText("Revoke Key A?")).toBeNull());

    fireEvent.click(revokeOf("Key B"));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Revoke Key B" })).toBeNull());

    // A's reload (which saw B still active) arrives last and must be ignored.
    await act(async () => slow.resolve(json({})));
    await new Promise((r) => setTimeout(r, 10));
    expect(screen.queryByRole("button", { name: "Revoke Key B" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revoke Key A" })).toBeNull();
    for (const status of screen.getAllByTestId("api-key-status")) expect(status).toHaveTextContent("Revoked");
  });
});

describe("picker retry", () => {
  it("a failed agents/workflows load offers Retry, which loads them", async () => {
    let agentCalls = 0;
    handler = (url, init) => {
      if (isList(url, init)) return json({ items: [] });
      if (url.endsWith("/api/v1/agents")) {
        agentCalls += 1;
        return agentCalls === 1 ? json({ detail: "agents down" }, 500) : json([{ id: "a1", name: "Helper" }]);
      }
      if (url.endsWith("/api/v1/workflows")) return json([]);
      return json({ detail: "nope" }, 404);
    };
    render(<ApiKeysSection />);
    await screen.findByTestId("api-keys-empty");
    fireEvent.click(screen.getByTestId("api-key-create"));
    const dialog = await screen.findByTestId("api-key-create-dialog");
    fireEvent.click(within(dialog).getByTestId("api-key-access-some"));
    expect(await within(dialog).findByText("agents down")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByTestId("api-key-targets-retry"));
    expect(await within(dialog).findByText("Helper")).toBeInTheDocument();
    expect(within(dialog).queryByText("agents down")).toBeNull();
    expect(agentCalls).toBe(2);
  });
});
