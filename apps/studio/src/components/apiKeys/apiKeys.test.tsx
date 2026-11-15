import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

import { ApiKeysSection } from "./ApiKeysSection";
import { ConnectAgUiAction } from "./ConnectAgUiSheet";
import type { ApiKey } from "@/lib/apiKeys";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { nextRouter } from "@/test/nextRouterMock";

const SECRET = "pos_0123456789abcdef01234567_" + "s".repeat(43);

function key(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: "0123456789abcdef01234567",
    name: "Support site",
    prefix: "pos_0123456789abcdef01234567_",
    status: "active",
    scopes: ["agui:run"],
    agent_ids: null,
    workflow_ids: null,
    allowed_origins: null,
    created_by: "u1",
    created_at: "2026-11-15T10:00:00Z",
    expires_at: null,
    revoked_at: null,
    last_used_at: null,
    ...overrides,
  };
}

let stored: ApiKey[];
let fetchMock: ReturnType<typeof vi.fn>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
  stored = [key({ id: "old", name: "Old key", prefix: "pos_old_", status: "revoked", revoked_at: "2026-11-14T00:00:00Z" })];
  fetchMock = vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = String(input);
    if (url.endsWith("/api/v1/api-keys") && (init.method ?? "GET") === "GET") return json({ items: stored });
    if (url.endsWith("/api/v1/api-keys") && init.method === "POST") {
      const body = JSON.parse(String(init.body));
      const row = key({ name: body.name });
      stored = [row, ...stored];
      return json({ ...row, key: SECRET }, 201);
    }
    const revoke = /\/api\/v1\/api-keys\/([^/]+)$/.exec(url);
    if (revoke && init.method === "DELETE") {
      stored = stored.map((item) => (item.id === revoke[1] ? { ...item, status: "revoked", revoked_at: "2026-11-15T11:00:00Z" } : item));
      return new Response(null, { status: 204 });
    }
    return json({ detail: "not found" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("ApiKeysSection", () => {
  it("shows the new key once; after closing (or a reload) it is gone", async () => {
    const { unmount } = render(<ApiKeysSection />);
    expect(await screen.findByText("Old key")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("api-key-create"));
    const dialog = await screen.findByTestId("api-key-create-dialog");
    fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: "Support site" } });
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));

    const secret = await screen.findByTestId("api-key-secret");
    expect(within(secret).getByDisplayValue(SECRET)).toBeInTheDocument();
    // The list behind the dialog has the new row, without the secret.
    expect(screen.getByTestId("api-keys-list").textContent).not.toContain(SECRET);

    fireEvent.click(screen.getByTestId("api-key-done"));
    await waitFor(() => expect(screen.queryByTestId("api-key-create-dialog")).toBeNull());
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(screen.getAllByTestId("api-key-row")).toHaveLength(2);

    unmount();
    render(<ApiKeysSection />);
    expect(await screen.findByText("Support site")).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(window.localStorage.length).toBe(0);
  });

  it("revokes after confirming; Cancel keeps the key", async () => {
    stored = [key()];
    render(<ApiKeysSection />);
    fireEvent.click(await screen.findByTestId("api-key-revoke"));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);

    fireEvent.click(screen.getByTestId("api-key-revoke"));
    expect(await screen.findByText("Revoke Support site?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.getByTestId("api-key-status")).toHaveTextContent("Revoked"));
    expect(screen.queryByTestId("api-key-revoke")).toBeNull();
    expect(screen.getByTestId("api-keys-status")).toHaveTextContent("revoked");
  });

  it("refuses a picked-access key with nothing picked", async () => {
    fetchMock.mockImplementationOnce(async () => json({ items: [] }));
    render(<ApiKeysSection />);
    expect(await screen.findByTestId("api-keys-empty")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("api-key-create"));
    const dialog = await screen.findByTestId("api-key-create-dialog");
    fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: "x" } });
    fireEvent.click(within(dialog).getByTestId("api-key-access-some"));
    fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
    expect(await within(dialog).findByTestId("api-key-create-error")).toHaveTextContent("Pick at least one");
  });

  it("has no axe violations", async () => {
    stored = [key()];
    const { container } = render(<ApiKeysSection />);
    await screen.findByTestId("api-key-row");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("ConnectAgUiAction", () => {
  beforeEach(() => nextRouter.reset(["/agents/a1/chat"]));

  it("opens a URL-backed Sheet with the URL and snippets, never a key", async () => {
    render(<ConnectAgUiAction target={{ kind: "agent", id: "a1" }} name="Helper" />);
    fireEvent.click(screen.getByTestId("connect-agui-open"));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/agents/a1/chat?connect=ag-ui");
    const sheet = await screen.findByTestId("connect-agui-sheet");
    expect(within(sheet).getByDisplayValue(/\/api\/v1\/ag-ui\/agents\/a1$/)).toBeInTheDocument();
    expect(within(sheet).getByTestId("connect-agui-copilotkit").textContent).toContain("/api/v1/ag-ui/agents/a1");
    expect(within(sheet).getByTestId("connect-agui-keys-link")).toHaveAttribute("href", routes.settingsApiKeys());
    expect(sheet.innerHTML).not.toMatch(/pos_[0-9a-f]/);
    // The Sheet never asks for keys.
    expect(fetchMock).not.toHaveBeenCalled();
    // Closing goes Back: the Sheet's history entry is its own.
    fireEvent.click(within(sheet).getByRole("button", { name: "Close" }));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/agents/a1/chat");
    await waitFor(() => expect(screen.queryByTestId("connect-agui-sheet")).toBeNull());
  });

  it("uses the workflow endpoint", async () => {
    nextRouter.reset(["/workflows/wf1?connect=ag-ui"]);
    render(<ConnectAgUiAction target={{ kind: "workflow", id: "wf1" }} name="Onboarding" />);
    const sheet = await screen.findByTestId("connect-agui-sheet");
    expect(within(sheet).getByDisplayValue(/\/api\/v1\/ag-ui\/workflows\/wf1$/)).toBeInTheDocument();
  });
});
