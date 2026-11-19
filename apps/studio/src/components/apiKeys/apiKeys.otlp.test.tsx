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
import {
  buildApiKeyCreate,
  hasOtlpScope,
  otlpEnvSnippet,
  otlpJavaScriptSnippet,
  otlpPythonSnippet,
  otlpTracesEndpoint,
  type ApiKey,
} from "@/lib/apiKeys";

function key(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: "0123456789abcdef01234567", name: "Operator", prefix: "pos_0123456789abcdef01234567_", status: "active",
    scopes: ["agui:run"], agent_ids: null, workflow_ids: null, allowed_origins: null, created_by: "u1",
    created_at: "2026-11-15T10:00:00Z", expires_at: null, revoked_at: null, last_used_at: null, ...overrides,
  };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let posted: Array<Record<string, unknown>>;

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
  posted = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit = {}) => {
    const url = String(input);
    if (url.endsWith("/api/v1/api-keys") && (init.method ?? "GET") === "GET") return json({ items: [key({ id: "o", name: "Exporter", scopes: ["otlp"] }), key()] });
    if (url.endsWith("/api/v1/api-keys") && init.method === "POST") {
      const body = JSON.parse(String(init.body));
      posted.push(body);
      return json({ ...key({ id: "new", name: body.name, scopes: body.scopes ?? ["agui:run"] }), key: "pos_new_" + "s".repeat(43) }, 201);
    }
    return json({ detail: "not found" }, 404);
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("OTLP key scope and connect snippets", () => {
  it("builds the scopes for the otlp toggle", () => {
    const base = { name: "x", access: "all" as const, agentIds: [], workflowIds: [], origins: "", expiresAt: null };
    expect(buildApiKeyCreate({ ...base })).toEqual({ ok: true, body: { name: "x" } });
    expect(buildApiKeyCreate({ ...base, otlp: true })).toEqual({ ok: true, body: { name: "x", scopes: ["agui:run", "otlp"] } });
    expect(buildApiKeyCreate({ ...base, mcp: true, otlp: true })).toEqual({ ok: true, body: { name: "x", scopes: ["agui:run", "mcp", "otlp"] } });
    expect(hasOtlpScope({ scopes: ["otlp"] })).toBe(true);
    expect(hasOtlpScope({ scopes: ["mcp", "manage"] })).toBe(false);
  });

  it("names the standard OpenTelemetry variables and never a key value", () => {
    const base = "https://studio.example.com";
    expect(otlpTracesEndpoint(base + "/")).toBe("https://studio.example.com/api/v1/otlp/v1/traces");
    const env = otlpEnvSnippet(base);
    expect(env).toContain("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://studio.example.com/api/v1/otlp/v1/traces");
    expect(env).toContain('OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $PYDANTICOS_API_KEY"');
    expect(env).toContain("OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf");
    expect(otlpPythonSnippet()).toContain("OTLPSpanExporter");
    expect(otlpPythonSnippet()).toContain("pydanticos.run_id");
    expect(otlpJavaScriptSnippet()).toContain("@opentelemetry/exporter-trace-otlp-proto");
    for (const snippet of [env, otlpPythonSnippet(), otlpJavaScriptSnippet()]) expect(snippet).not.toMatch(/pos_[0-9a-f]/);
  });

  it("shows the connect panel with the endpoint and the Python and JavaScript snippets", async () => {
    render(<ApiKeysSection />);
    const panel = await screen.findByTestId("settings-otlp-connect");
    expect(within(panel).getByDisplayValue(/\/api\/v1\/otlp\/v1\/traces$/)).toBeInTheDocument();
    expect(screen.getByTestId("otlp-snippet-env").textContent).toContain("OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf");
    expect(screen.getByTestId("otlp-snippet-python").textContent).toContain("BatchSpanProcessor");
    expect(screen.getByTestId("otlp-snippet-javascript").textContent).toContain("NodeSDK");
    expect(panel.innerHTML).not.toMatch(/pos_[0-9a-f]/);
  });

  it("marks a key with the otlp scope and sends the scope only when the toggle is on", async () => {
    render(<ApiKeysSection />);
    const rows = await screen.findAllByTestId("api-key-row");
    expect(within(rows[0]).getByTestId("api-key-otlp")).toHaveTextContent("OTLP");
    expect(within(rows[1]).queryByTestId("api-key-otlp")).toBeNull();
    for (const [name, on] of [["Plain", false], ["Exporter two", true]] as const) {
      fireEvent.click(screen.getByTestId("api-key-create"));
      const dialog = await screen.findByTestId("api-key-create-dialog");
      fireEvent.change(within(dialog).getByTestId("api-key-name"), { target: { value: name } });
      if (on) fireEvent.click(within(dialog).getByTestId("api-key-otlp-scope"));
      fireEvent.click(within(dialog).getByTestId("api-key-create-submit"));
      fireEvent.click(await screen.findByTestId("api-key-done"));
      await waitFor(() => expect(screen.queryByTestId("api-key-create-dialog")).toBeNull());
      expect(posted.at(-1)?.scopes).toEqual(on ? ["agui:run", "otlp"] : undefined);
    }
  });
});
