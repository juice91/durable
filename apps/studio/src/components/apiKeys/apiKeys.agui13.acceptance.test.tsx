/** AGUI.13 acceptance: key lifecycle, list and revoke, scopes in the dialog, dev-only message, layout. */
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiKeysSection } from "./ApiKeysSection";
import type { ApiKey } from "@/lib/apiKeys";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

const SECRET = "pos_0123456789abcdef01234567_" + "Z".repeat(43);
const mk = (o: Partial<ApiKey> = {}): ApiKey => ({
  id: "k1", name: "Support site", prefix: "pos_k1_", status: "active", scopes: ["agui:run"], agent_ids: null,
  workflow_ids: null, allowed_origins: null, created_by: "u1", created_at: "2026-11-15T10:00:00Z",
  expires_at: null, revoked_at: null, last_used_at: null, ...o,
});
const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });

let stored: ApiKey[];
let posts: Record<string, unknown>[];
let revokeResponse: () => Response;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
  stored = [];
  posts = [];
  revokeResponse = () => new Response(null, { status: 204 });
  fetchMock = vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = String(input);
    const method = init.method ?? "GET";
    if (url.endsWith("/api/v1/agents")) return json([{ id: "a1", name: "Zed" }, { id: "a2", name: "Alpha" }]);
    if (url.endsWith("/api/v1/workflows")) return json([{ id: "w1", name: "Flow" }]);
    if (url.endsWith("/api/v1/api-keys") && method === "GET") return json({ items: stored });
    if (url.endsWith("/api/v1/api-keys") && method === "POST") {
      const body = JSON.parse(String(init.body));
      posts.push(body);
      const row = mk({ id: "new", name: body.name, prefix: "pos_new_", agent_ids: body.agent_ids ?? null, workflow_ids: body.workflow_ids ?? null, allowed_origins: body.allowed_origins ?? null });
      stored = [row, ...stored];
      return json({ ...row, key: SECRET }, 201);
    }
    if (method === "DELETE") return revokeResponse();
    return json({ detail: "not found" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  window.localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

async function openDialog() {
  fireEvent.click(await screen.findByTestId("api-key-create"));
  return screen.findByTestId("api-key-create-dialog");
}
const submitNamed = async (name: string) => {
  const dialog = await openDialog();
  fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: name } });
  fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
  return dialog;
};
function secretLocations(): string[] {
  const where: string[] = [];
  if (document.documentElement.outerHTML.includes(SECRET)) where.push("dom");
  if (document.body.textContent?.includes(SECRET)) where.push("text");
  if (Array.from(document.querySelectorAll("input,textarea")).some((e) => (e as HTMLInputElement).value.includes(SECRET))) where.push("field value");
  for (const store of [window.localStorage, window.sessionStorage]) {
    for (let i = 0; i < store.length; i++) if ((store.getItem(store.key(i)!) ?? "").includes(SECRET) || store.key(i)!.includes(SECRET)) where.push("storage");
  }
  if (window.location.href.includes(SECRET)) where.push("url");
  if (document.title.includes(SECRET)) where.push("title");
  return where;
}

describe("key lifecycle", () => {
  it("the secret is in exactly one place (the dialog's field) while shown, nowhere after Done", async () => {
    render(<ApiKeysSection />);
    await submitNamed("Support site");
    const field = await screen.findByTestId("api-key-secret");
    expect((within(field).getByDisplayValue(SECRET) as HTMLInputElement).readOnly).toBe(true);
    expect(document.documentElement.outerHTML.split(SECRET).length - 1).toBeLessThanOrEqual(1);
    expect(screen.getByTestId("api-keys-list").textContent).not.toContain(SECRET);
    expect(screen.getByTestId("api-keys-status").textContent).not.toContain(SECRET);
    expect(window.localStorage.length + window.sessionStorage.length).toBe(0);
    expect(window.location.href).not.toContain(SECRET);

    fireEvent.click(screen.getByTestId("api-key-done"));
    await waitFor(() => expect(screen.queryByTestId("api-key-create-dialog")).toBeNull());
    expect(secretLocations()).toEqual([]);
    // Re-opening the dialog starts from a blank form: no earlier secret or name.
    const again = await openDialog();
    expect((within(again).getByTestId("api-key-name") as HTMLInputElement).value).toBe("");
    expect(secretLocations()).toEqual([]);
  });

  it("Escape and the Close X also drop the secret", async () => {
    render(<ApiKeysSection />);
    await submitNamed("A");
    await screen.findByTestId("api-key-secret");
    fireEvent.keyDown(screen.getByTestId("api-key-create-dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("api-key-create-dialog")).toBeNull());
    expect(secretLocations()).toEqual([]);

    await submitNamed("B");
    const dialog = await screen.findByTestId("api-key-create-dialog");
    await within(dialog).findByTestId("api-key-secret");
    expect(secretLocations()).not.toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByTestId("api-key-create-dialog")).toBeNull());
    expect(secretLocations()).toEqual([]);
  });

  it("a failed create shows the API's message, no secret, and the form stays editable", async () => {
    fetchMock.mockImplementation(async (input: string, init: RequestInit = {}) =>
      String(input).endsWith("/api-keys") && init.method === "POST" ? json({ detail: "Too many keys" }, 409) : json({ items: [] }));
    render(<ApiKeysSection />);
    const dialog = await submitNamed("A");
    expect(await within(dialog).findByTestId("api-key-create-error")).toHaveTextContent("Too many keys");
    expect(screen.queryByTestId("api-key-secret")).toBeNull();
    expect(within(dialog).getByTestId("api-key-create-submit")).not.toBeDisabled();
    expect(secretLocations()).toEqual([]);
  });

  it("the list never keeps the key after create, even though the POST answer had it", async () => {
    render(<ApiKeysSection />);
    await submitNamed("Support site");
    await screen.findByTestId("api-key-secret");
    const row = await screen.findByTestId("api-key-row");
    expect(row.textContent).not.toContain(SECRET);
    expect(within(row).getByTestId("api-key-prefix")).toHaveTextContent("pos_new_");
  });
});
