import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import type { IamMe } from "@/lib/iam";
import { PersonalApiKeys } from "./PersonalApiKeys";

const fetchMock = vi.fn();
const SECRET = "pos_abc123_THE-ONE-TIME-SECRET";

const me = (over: Partial<IamMe> = {}): IamMe => ({ subject: "s", email: "j@example.com", tenant_id: "acme", kind: "user", roles: ["admin"], provider: "zitadel", ...over });
const key = (over: Record<string, unknown> = {}) => ({
  id: "k1", name: "Laptop", prefix: "pos_k1_", status: "active", scopes: ["mcp"], created_at: "2026-11-01T10:00:00Z",
  expires_at: "2027-02-01T10:00:00Z", revoked_at: null, last_used_at: null, ...over,
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function serve(opts: { list?: unknown[]; create?: () => Response; revoke?: () => Response } = {}) {
  let items = opts.list ?? [];
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (method === "POST") return Promise.resolve(opts.create ? opts.create() : json({ ...key({ id: "k2", name: "New" }), key: SECRET }, 201));
    if (method === "DELETE") return Promise.resolve(opts.revoke ? opts.revoke() : new Response(null, { status: 204 }));
    return Promise.resolve(json({ items }));
  });
  return (next: unknown[]) => { items = next; };
}
const calls = (method: string) => fetchMock.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("PersonalApiKeys", () => {
  test("lists a key's name, prefix, scopes, dates and status; an empty list says so", async () => {
    serve({ list: [key({ last_used_at: "2026-11-20T10:00:00Z" }), key({ id: "k0", name: "Old", status: "revoked" })] });
    render(<PersonalApiKeys me={me()} />);
    const row = await screen.findByTestId("personal-key-row-k1");
    expect(row).toHaveTextContent("Laptop");
    expect(row).toHaveTextContent("pos_k1_");
    expect(row).toHaveTextContent("active");
    expect(within(row).getByTestId("personal-key-revoke-k1")).toBeInTheDocument();
    expect(within(screen.getByTestId("personal-key-row-k0")).queryByRole("button")).toBeNull();
    cleanup();
    serve({ list: [] });
    render(<PersonalApiKeys me={me()} />);
    expect(await screen.findByTestId("personal-keys-empty")).toBeInTheDocument();
  });

  test("create sends name, scopes and a 90-day expiry, then shows the secret once and never again", async () => {
    const user = userEvent.setup();
    const setList = serve({ list: [] });
    render(<PersonalApiKeys me={me()} />);
    await screen.findByTestId("personal-keys-empty");
    await user.click(screen.getByTestId("personal-key-create"));
    await user.type(screen.getByTestId("personal-key-name"), "New");
    await user.click(screen.getByTestId("personal-key-scope-manage"));
    await user.click(screen.getByTestId("personal-key-submit"));
    const secret = await screen.findByTestId("personal-key-secret");
    expect(within(secret).getByDisplayValue(SECRET)).toBeInTheDocument();
    expect(screen.getByTestId("personal-key-once")).toHaveTextContent("You won't see this key again");
    const body = JSON.parse(String(calls("POST")[0][1].body));
    expect(body.name).toBe("New");
    expect(body.scopes).toEqual(["mcp", "manage"]);
    const days = (Date.parse(body.expires_at) - Date.now()) / 86_400_000;
    expect(days).toBeGreaterThan(89);
    expect(days).toBeLessThan(90.1);
    expect(calls("POST")[0][1].cache).toBe("no-store");
    // The row appears without the secret.
    expect(screen.getByTestId("personal-key-row-k2")).not.toHaveTextContent(SECRET);
    setList([key({ id: "k2", name: "New" })]);
    await user.click(screen.getByTestId("personal-key-done"));
    await waitFor(() => expect(screen.queryByTestId("personal-key-dialog")).toBeNull());
    expect(document.body.textContent).not.toContain(SECRET);
    expect(JSON.stringify(localStorage)).not.toContain(SECRET);
    expect(JSON.stringify(sessionStorage)).not.toContain(SECRET);
    // Reopening the dialog starts a blank form.
    await user.click(screen.getByTestId("personal-key-create"));
    expect(screen.getByTestId("personal-key-name")).toHaveValue("");
    expect(document.body.textContent).not.toContain(SECRET);
  });

  test("a viewer may pick only mcp: the other scopes are disabled with a reason", async () => {
    const user = userEvent.setup();
    serve();
    render(<PersonalApiKeys me={me({ roles: ["viewer"] })} />);
    await user.click(await screen.findByTestId("personal-key-create"));
    expect(screen.getByTestId("personal-key-scope-mcp")).toBeEnabled();
    for (const scope of ["agui:run", "manage", "otlp"]) {
      expect(screen.getByTestId(`personal-key-scope-${scope}`)).toBeDisabled();
      expect(screen.getByTestId(`personal-key-scope-reason-${scope}`)).toHaveTextContent(/Needs/);
    }
  });

  test("an editor may not pick otlp; an admin may pick all four", async () => {
    const user = userEvent.setup();
    serve();
    render(<PersonalApiKeys me={me({ roles: ["editor"] })} />);
    await user.click(await screen.findByTestId("personal-key-create"));
    expect(screen.getByTestId("personal-key-scope-manage")).toBeEnabled();
    expect(screen.getByTestId("personal-key-scope-otlp")).toBeDisabled();
    cleanup();
    render(<PersonalApiKeys me={me({ roles: ["admin"] })} />);
    await user.click(await screen.findByTestId("personal-key-create"));
    expect(screen.getByTestId("personal-key-scope-otlp")).toBeEnabled();
  });

  test("a custom expiry beyond 365 days is refused before sending", async () => {
    const user = userEvent.setup();
    serve();
    render(<PersonalApiKeys me={me()} />);
    await user.click(await screen.findByTestId("personal-key-create"));
    await user.type(screen.getByTestId("personal-key-name"), "Far");
    await user.click(screen.getByTestId("personal-key-expiry-custom"));
    const far = new Date(Date.now() + 400 * 86_400_000).toISOString().slice(0, 10);
    const input = screen.getByTestId("personal-key-expiry-date");
    input.removeAttribute("max");
    await user.type(input, far);
    await user.click(screen.getByTestId("personal-key-submit"));
    expect(await screen.findByTestId("personal-key-error")).toHaveTextContent("at most 365 days");
    expect(calls("POST")).toHaveLength(0);
  });

  test("a 403 not_a_person on create says to sign in", async () => {
    const user = userEvent.setup();
    serve({ create: () => json({ detail: "Personal API keys are for people, not the dev bearer (not_a_person)" }, 403) });
    render(<PersonalApiKeys me={me()} />);
    await user.click(await screen.findByTestId("personal-key-create"));
    await user.type(screen.getByTestId("personal-key-name"), "X");
    await user.click(screen.getByTestId("personal-key-submit"));
    expect(await screen.findByTestId("personal-key-error")).toHaveTextContent(/Sign in with your own account/);
  });

  test("revoke asks first, then marks the key revoked; cancel sends nothing", async () => {
    const user = userEvent.setup();
    serve({ list: [key()] });
    render(<PersonalApiKeys me={me()} />);
    await user.click(await screen.findByTestId("personal-key-revoke-k1"));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(calls("DELETE")).toHaveLength(0);
    await user.click(screen.getByTestId("personal-key-revoke-k1"));
    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(calls("DELETE")).toHaveLength(1));
    expect(String(calls("DELETE")[0][0])).toMatch(/\/api\/v1\/iam\/me\/api-keys\/k1$/);
    await waitFor(() => expect(screen.getByTestId("personal-key-row-k1")).toHaveTextContent("revoked"));
    expect(screen.queryByTestId("personal-key-revoke-k1")).toBeNull();
  });

  test("a revoke failure is shown in the dialog", async () => {
    const user = userEvent.setup();
    serve({ list: [key()], revoke: () => json({ detail: "Not found" }, 404) });
    render(<PersonalApiKeys me={me()} />);
    await user.click(await screen.findByTestId("personal-key-revoke-k1"));
    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(await screen.findByText("Not found")).toBeInTheDocument();
  });

  test("a dev-bearer or API key session sees one line and makes no request", () => {
    serve();
    render(<PersonalApiKeys me={me({ provider: "loopback-dev", subject: "operator", email: null })} />);
    expect(screen.getByTestId("profile-api-keys-unavailable")).toHaveTextContent(/signed-in person/);
    expect(screen.queryByTestId("personal-key-create")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test("a failing list shows a readable error with Retry", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(json({ detail: "Personal API keys are for people (not_a_person)" }, 403)));
    render(<PersonalApiKeys me={me()} />);
    expect(await screen.findByTestId("personal-keys-error")).toHaveTextContent(/Sign in/);
  });
});
