/** AGUI.13 acceptance: list metadata, revoke paths, picked scopes in the dialog, dev-only message, layout. */
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiKeysSection } from "./ApiKeysSection";
import type { ApiKey } from "@/lib/apiKeys";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

const mk = (o: Partial<ApiKey> = {}): ApiKey => ({
  id: "k1", name: "Support site", prefix: "pos_k1_", status: "active", scopes: ["agui:run"], agent_ids: null,
  workflow_ids: null, allowed_origins: null, created_by: "u1", created_at: "2026-11-15T10:00:00Z",
  expires_at: null, revoked_at: null, last_used_at: null, ...o,
});
const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });

let stored: ApiKey[];
let posts: Record<string, unknown>[];
let deleteAnswer: () => Response;
let listAnswer: (() => Response) | null;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
  stored = [];
  posts = [];
  listAnswer = null;
  deleteAnswer = () => {
    stored = stored.map((k) => ({ ...k, status: "revoked", revoked_at: "2026-11-15T11:00:00Z" }));
    return new Response(null, { status: 204 });
  };
  fetchMock = vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = String(input);
    const method = init.method ?? "GET";
    if (url.endsWith("/api/v1/agents")) return json([{ id: "a1", name: "Zed" }, { id: "a2", name: "Alpha" }]);
    if (url.endsWith("/api/v1/workflows")) return json([{ id: "w1", name: "Flow" }]);
    if (url.endsWith("/api/v1/api-keys") && method === "GET") return listAnswer ? listAnswer() : json({ items: stored });
    if (url.endsWith("/api/v1/api-keys") && method === "POST") {
      posts.push(JSON.parse(String(init.body)));
      return json({ ...mk({ id: "n" }), key: "pos_n_secret" }, 201);
    }
    if (method === "DELETE") return deleteAnswer();
    return json({ detail: "nope" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});
const deletes = () => fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE");

describe("list: prefix and metadata only", () => {
  it("shows name, prefix, status, access, origins and dates; no secret-looking value or hash", async () => {
    stored = [
      mk({ allowed_origins: ["https://a.example.com"], agent_ids: ["a1"], workflow_ids: [], expires_at: "2027-01-01T00:00:00Z" }),
      mk({ id: "k2", name: "Dead", prefix: "pos_k2_", status: "revoked", revoked_at: "2026-11-14T00:00:00Z" }),
      mk({ id: "k3", name: "Old", prefix: "pos_k3_", status: "expired", expires_at: "2026-01-01T00:00:00Z" }),
    ];
    render(<ApiKeysSection />);
    const rows = await screen.findAllByTestId("api-key-row");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByTestId("api-key-prefix")).toHaveTextContent("pos_k1_");
    expect(rows[0]).toHaveTextContent("1 agent, 0 workflows");
    expect(rows[0]).toHaveTextContent("https://a.example.com");
    expect(rows[0]).toHaveTextContent("Never");
    expect(within(rows[1]).getByTestId("api-key-status")).toHaveTextContent("Revoked");
    expect(within(rows[2]).getByTestId("api-key-status")).toHaveTextContent("Expired");
    // Only active keys can be revoked.
    expect(screen.getAllByTestId("api-key-revoke")).toHaveLength(1);
    expect(screen.getByTestId("api-key-revoke")).toHaveAccessibleName("Revoke Support site");
    expect(screen.getByTestId("api-keys-list").textContent).not.toMatch(/pos_[0-9a-f]{6,}_?\w{10}/);
  });
  it("an empty list says so", async () => {
    render(<ApiKeysSection />);
    expect(await screen.findByTestId("api-keys-empty")).toBeInTheDocument();
  });
});

describe("revoke", () => {
  it("asks first, sends nothing until confirmed, then reloads the list", async () => {
    stored = [mk()];
    render(<ApiKeysSection />);
    fireEvent.click(await screen.findByTestId("api-key-revoke"));
    expect(await screen.findByText("Revoke Support site?")).toBeInTheDocument();
    expect(deletes()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.getByTestId("api-key-status")).toHaveTextContent("Revoked"));
    expect(deletes()).toHaveLength(1);
    expect(String(deletes()[0][0])).toMatch(/\/api\/v1\/api-keys\/k1$/);
    expect(screen.queryByText("Revoke Support site?")).toBeNull();
  });
  it("a server error keeps the dialog open with the message and the key stays active", async () => {
    stored = [mk()];
    deleteAnswer = () => json({ detail: "Database down" }, 500);
    render(<ApiKeysSection />);
    fireEvent.click(await screen.findByTestId("api-key-revoke"));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(await screen.findByText("Database down")).toBeInTheDocument();
    expect(screen.getByText("Revoke Support site?")).toBeInTheDocument();
    expect(screen.getByTestId("api-key-status")).toHaveTextContent("Active");
  });
  it("a 404 (already gone) says so and reloads", async () => {
    stored = [mk()];
    deleteAnswer = () => {
      stored = [];
      return json({ detail: "API key not found" }, 404);
    };
    render(<ApiKeysSection />);
    fireEvent.click(await screen.findByTestId("api-key-revoke"));
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.getByTestId("api-keys-status")).toHaveTextContent("no longer exists"));
    expect(await screen.findByTestId("api-keys-empty")).toBeInTheDocument();
  });
});

async function openCreate() {
  fireEvent.click(await screen.findByTestId("api-key-create"));
  return screen.findByTestId("api-key-create-dialog");
}

describe("scopes and allowlists in the dialog", () => {
  it("picked access posts both lists (the unpicked kind empty), origins and a UTC expiry", async () => {
    render(<ApiKeysSection />);
    const dialog = await openCreate();
    fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: "Site" } });
    fireEvent.click(within(dialog).getByTestId("api-key-access-some"));
    const agents = await within(dialog).findByTestId("api-key-agents");
    // Sorted by name, so Alpha is first.
    expect(within(agents).getAllByRole("checkbox").map((c) => c.closest("label")!.textContent)).toEqual(["Alpha", "Zed"]);
    fireEvent.click(within(agents).getByLabelText("Alpha"));
    fireEvent.change(within(dialog).getByTestId("api-key-origins"), { target: { value: "https://a.example.com\nhttps://b.example.com" } });
    fireEvent.change(within(dialog).getByTestId("api-key-expires"), { target: { value: "2099-01-02T03:04" } });
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ name: "Site", agent_ids: ["a2"], workflow_ids: [], allowed_origins: ["https://a.example.com", "https://b.example.com"] });
    expect(String(posts[0].expires_at)).toMatch(/^2099-01-0\dT.*Z$/);
  });
  it("all access posts no allowlist field; the picker is not even loaded", async () => {
    render(<ApiKeysSection />);
    const dialog = await openCreate();
    fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: "All" } });
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ name: "All" });
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith("/api/v1/agents"))).toBe(false);
  });
  it("a past expiry blocks the request with a message", async () => {
    render(<ApiKeysSection />);
    const dialog = await openCreate();
    fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: "x" } });
    fireEvent.change(within(dialog).getByTestId("api-key-expires"), { target: { value: "2001-01-01T00:00" } });
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
    expect(await within(dialog).findByTestId("api-key-create-error")).toHaveTextContent("must be in the future");
    expect(posts).toHaveLength(0);
  });
  it("a bad origin is judged by the server: its 422 shows in the dialog and no secret appears", async () => {
    const detail = "wildcards are not allowed in an origin; list each origin: 'https://*.x.com'";
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input: string, init: RequestInit = {}) =>
      String(input).endsWith("/api/v1/api-keys") && init.method === "POST" ? json({ detail }, 422) : (base as (i: string, o?: RequestInit) => unknown)(input, init));
    render(<ApiKeysSection />);
    const dialog = await openCreate();
    fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: "x" } });
    fireEvent.change(within(dialog).getByTestId("api-key-origins"), { target: { value: "https://*.x.com" } });
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
    expect(await within(dialog).findByTestId("api-key-create-error")).toHaveTextContent(detail);
    expect(screen.queryByTestId("api-key-secret")).toBeNull();
  });
  it("a failed agent list shows an alert and picking nothing still cannot create", async () => {
    fetchMock.mockImplementation(async (input: string) => (String(input).endsWith("/agents") ? json({ detail: "agents down" }, 500) : json({ items: [] })));
    render(<ApiKeysSection />);
    const dialog = await openCreate();
    fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: "x" } });
    fireEvent.click(within(dialog).getByTestId("api-key-access-some"));
    expect(await within(dialog).findByText("agents down")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
    expect(await within(dialog).findByTestId("api-key-create-error")).toHaveTextContent("Pick at least one");
    expect(posts).toHaveLength(0);
  });
});

describe("dev-only management", () => {
  it("shows a tailored, non-retryable message when the API refuses management (api_key mode, 401)", async () => {
    listAnswer = () => json({ detail: "dev bearer authentication is not enabled" }, 401);
    render(<ApiKeysSection />);
    const error = await screen.findByTestId("api-keys-error");
    expect(error).toHaveAttribute("role", "alert");
    expect(error).toHaveTextContent("API keys can be managed only when the API runs in dev mode (EAP_AUTH_MODE=dev).");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByTestId("api-key-create")).toBeNull();
    expect(screen.queryByTestId("api-keys-list")).toBeNull();
  });
  it("Retry reloads", async () => {
    listAnswer = () => json({ detail: "boom" }, 500);
    render(<ApiKeysSection />);
    await screen.findByTestId("api-keys-error");
    stored = [mk()];
    listAnswer = null;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("api-key-row")).toBeInTheDocument();
    expect(screen.queryByTestId("api-keys-error")).toBeNull();
  });
});

describe("layout, axe, keyboard", () => {
  it("375 px: no fixed width over 375, no horizontal-scroll classes on the page, long values wrap", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 375 });
    stored = [mk({ name: "N".repeat(120), prefix: "pos_" + "a".repeat(60) + "_", allowed_origins: ["https://" + "x".repeat(100) + ".example.com"] })];
    const { container } = render(<ApiKeysSection />);
    await screen.findByTestId("api-key-row");
    const all = Array.from(container.querySelectorAll<HTMLElement>("*"));
    expect(all.filter((el) => Array.from(el.classList).some((c) => /^(min-)?w-\[(\d+)px\]$/.test(c) && Number(/\[(\d+)px\]/.exec(c)![1]) > 375))).toEqual([]);
    expect(all.filter((el) => Number.parseFloat(el.style.minWidth) > 375 || Number.parseFloat(el.style.width) > 375)).toEqual([]);
    const row = screen.getByTestId("api-key-row");
    expect(row.className).toMatch(/min-w-0/);
    expect(screen.getByTestId("api-key-prefix").className).toMatch(/break-all/);
    expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(document.documentElement.clientWidth || 375);
  });
  it("axe: list, create form, and the shown-once state", async () => {
    stored = [mk(), mk({ id: "k2", status: "revoked", name: "R" })];
    const { container } = render(<ApiKeysSection />);
    await screen.findAllByTestId("api-key-row");
    await expectNoAxeViolations(container);
    const dialog = await openCreate();
    await expectNoAxeViolations(dialog);
    fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: "x" } });
    fireEvent.click(within(dialog).getByTestId("api-key-access-some"));
    await expectNoAxeViolations(dialog);
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
    await screen.findByTestId("api-key-create-error");
    await expectNoAxeViolations(screen.getByTestId("api-key-create-dialog"));
    // The shown-once state: create for real, then check the secret view.
    fireEvent.click(within(dialog).getByTestId("api-key-access-all"));
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
    await screen.findByTestId("api-key-secret");
    await expectNoAxeViolations(screen.getByTestId("api-key-create-dialog"));
  }, AXE_TIMEOUT_MS);
  it("keyboard: controls are real buttons, the dialog is a modal dialog with a labelled name field, Enter submits", async () => {
    stored = [mk()];
    render(<ApiKeysSection />);
    const create = await screen.findByTestId("api-key-create");
    expect(create.tagName).toBe("BUTTON");
    expect(screen.getByTestId("api-key-revoke").tagName).toBe("BUTTON");
    fireEvent.click(create);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAccessibleName(/Create API key/);
    const name = within(dialog).getByLabelText("Name") as HTMLInputElement;
    fireEvent.change(name, { target: { value: "Kbd" } });
    fireEvent.submit(within(dialog).getByTestId("api-key-create-form"));
    await waitFor(() => expect(posts).toHaveLength(1));
    // After create, focus is inside the dialog (the Done/Copy buttons), not lost on body.
    await screen.findByTestId("api-key-done");
    await waitFor(() => expect(document.activeElement).not.toBe(document.body));
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toHaveTextContent("Copy");
    expect(within(screen.getByTestId("api-key-secret")).getByRole("button", { name: /Copy/ })).toBeInTheDocument();
  });
});
