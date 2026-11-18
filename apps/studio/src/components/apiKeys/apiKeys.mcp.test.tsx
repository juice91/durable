import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
import type { ApiKey } from "@/lib/apiKeys";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

function key(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: "0123456789abcdef01234567",
    name: "Operator",
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
let posted: Array<Record<string, unknown>>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
  stored = [key({ id: "m", name: "Manager", scopes: ["agui:run", "manage"] }), key()];
  posted = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = String(input);
      if (url.endsWith("/api/v1/api-keys") && (init.method ?? "GET") === "GET") return json({ items: stored });
      if (url.endsWith("/api/v1/api-keys") && init.method === "POST") {
        const body = JSON.parse(String(init.body));
        posted.push(body);
        const row = key({ id: "new", name: body.name, scopes: body.scopes ?? ["agui:run"] });
        return json({ ...row, key: "pos_new_" + "s".repeat(43) }, 201);
      }
      return json({ detail: "not found" }, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("API keys and MCP", () => {
  it("shows the MCP endpoint and the connect snippets, never a key", async () => {
    render(<ApiKeysSection />);
    const panel = await screen.findByTestId("settings-mcp-connect");
    expect(within(panel).getByDisplayValue(/\/api\/v1\/mcp$/)).toBeInTheDocument();
    expect(within(screen.getByTestId("mcp-snippet-claude")).getByText(/claude mcp add pydanticos --transport http .*\/api\/v1\/mcp/)).toBeInTheDocument();
    expect(screen.getByTestId("mcp-snippet-claude").textContent).toContain('--header "Authorization: Bearer $PYDANTICOS_API_KEY"');
    expect(screen.getByTestId("mcp-snippet-codex").textContent).toContain("bearer_token_env_var");
    expect(panel.innerHTML).not.toMatch(/pos_[0-9a-f]/);
  });

  it("marks a key that has the manage scope", async () => {
    render(<ApiKeysSection />);
    const rows = await screen.findAllByTestId("api-key-row");
    expect(within(rows[0]).getByTestId("api-key-manage")).toHaveTextContent("Manage");
    expect(within(rows[1]).queryByTestId("api-key-manage")).toBeNull();
  });

  it("sends the manage scope only when the toggle is on", async () => {
    render(<ApiKeysSection />);
    await screen.findAllByTestId("api-key-row");
    for (const [label, expected] of [["Run only", undefined], ["Reader", ["agui:run", "mcp"]], ["Operator", ["agui:run", "mcp", "manage"]]] as const) {
      fireEvent.click(screen.getByTestId("api-key-create"));
      const dialog = await screen.findByTestId("api-key-create-dialog");
      fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: label } });
      if (label === "Reader") fireEvent.click(within(dialog).getByTestId("api-key-mcp-scope"));
      if (label === "Operator") {
        fireEvent.click(within(dialog).getByTestId("api-key-manage-scope"));
        expect(within(dialog).getByTestId("api-key-mcp-scope")).toBeChecked();
        expect(within(dialog).getByTestId("api-key-mcp-scope")).toBeDisabled();
      }
      fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
      fireEvent.click(await screen.findByTestId("api-key-done"));
      await waitFor(() => expect(screen.queryByTestId("api-key-create-dialog")).toBeNull());
      expect(posted.at(-1)?.scopes).toEqual(expected);
    }
  });

  it("has no axe violations with the connect panel", async () => {
    const { container } = render(<ApiKeysSection />);
    await screen.findByTestId("settings-mcp-connect");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
