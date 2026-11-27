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

import { ExternalAccessPage } from "./ExternalAccessPage";
import type { A2AEndpoint, ExternalClient, ExternalSettings, WorkflowExport } from "@/lib/externalAccess";

const KEY = "pos_0123456789abcdef01234567_secretsecretsecretsecretsecretsecretsecr12";
const HASH = "b".repeat(64);

const settings: ExternalSettings = { enabled: false, base_url: "https://durable.example.test", grants_enforced: true, can_manage: true, max_runtime_seconds: 1209600 };

function client(over: Partial<ExternalClient> = {}): ExternalClient {
  return { id: "c1", name: "claude-code", description: "", enabled: true, max_active_runs: null, max_runs_per_day: null, revision: 3, created_at: "2026-11-26T10:00:00Z", keys: [], ...over };
}

function exported(over: Partial<WorkflowExport> = {}): WorkflowExport {
  return {
    id: "x1", name: "run_report", title: "Run the report", description: "d", tags: [], examples: [], workflow_id: "wf-1", workflow_name: "Report",
    version: "latest", resolved_version: 2, protocols: ["mcp"], result: "output", client_inputs: false, max_runtime_seconds: 3600,
    require_idempotency_key: true, enabled: false, disabled_reason: "export_contract_changed", enabled_by: null, enabled_at: null,
    approval_hash: HASH, input_schema: { type: "object", required: ["q"] },
    warnings: [{ code: "external_side_effects_without_approval", message: "has side effects without a person approving them" }],
    keys: [{ id: "k1", prefix: "pos_k1_", name: "laptop", status: "active", client_id: "c1", client_name: "claude-code" }], revision: 4, ...over,
  };
}

function anEndpoint(over: Partial<A2AEndpoint> = {}): A2AEndpoint {
  const endpointId = over.endpoint_id ?? "a".repeat(32);
  const url = `https://durable.example.test/api/v1/external/a2a/${endpointId}`;
  return {
    card_name: "Acme", card_description: "Workflows.", enabled: false, created_by: "user:alice", created_at: "2026-11-26T10:00:00Z", updated_at: "2026-11-26T10:00:00Z",
    ...over, endpoint_id: endpointId, url, card_url: `${url}/.well-known/agent-card.json`,
  };
}

let clients: ExternalClient[];
let exports_: WorkflowExport[];
let platform: ExternalSettings;
let endpoint: A2AEndpoint | null;
let calls: Array<{ url: string; method: string; body: unknown; ifMatch: string | null }>;

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
  clients = [];
  exports_ = [];
  platform = settings;
  endpoint = null;
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = String(input);
      const method = init.method ?? "GET";
      const body = init.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ url, method, body, ifMatch: new Headers(init.headers).get("If-Match") });
      if (url.endsWith("/api/v1/workflows")) return json([{ id: "wf-1", name: "Report", archived: false, latest_version: 2 }]);
      if (url.endsWith("/api/v1/agent-access")) return json(platform);
      if (url.endsWith("/agent-access/a2a-endpoint") && method === "GET") return json({ endpoint });
      if (url.endsWith("/agent-access/a2a-endpoint") && method === "PUT") {
        endpoint = anEndpoint(body as Partial<A2AEndpoint>);
        return json({ endpoint });
      }
      if (url.endsWith("/a2a-endpoint/rotate")) {
        endpoint = anEndpoint({ ...endpoint, endpoint_id: "f".repeat(32) });
        return json({ endpoint });
      }
      if (url.endsWith("/agent-access/clients") && method === "GET") return json({ items: clients });
      if (url.endsWith("/agent-access/clients") && method === "POST") {
        clients = [client({ name: body.name })];
        return json(clients[0], 201);
      }
      if (url.endsWith("/clients/c1") && method === "PATCH") {
        clients = [{ ...clients[0], enabled: body.enabled, revision: 4 }];
        return json(clients[0]);
      }
      if (url.endsWith("/clients/c1/keys") && method === "POST") {
        const made = { id: "k1", name: body.name, prefix: "pos_k1_", status: "active" as const, scopes: ["run"], workflow_ids: body.workflow_ids, expires_at: body.expires_at, last_used_at: null, created_at: "2026-11-26T10:00:00Z" };
        clients = [{ ...clients[0], keys: [made] }];
        return json({ ...made, key: KEY }, 201);
      }
      if (url.endsWith("/clients/c1/cancel-runs")) return json({ cancelled: ["r1", "r2"], pending: [] });
      if (url.endsWith("/agent-access/exports") && method === "GET") return json({ items: exports_ });
      if (url.endsWith("/agent-access/exports") && method === "POST") {
        exports_ = [exported({ name: body.name, title: body.title, disabled_reason: null })];
        return json(exports_[0], 201);
      }
      if (url.endsWith("/exports/x1/enable")) {
        exports_ = [{ ...exports_[0], enabled: true, disabled_reason: null, enabled_by: "user:alice", enabled_at: "2026-11-26T11:00:00Z" }];
        return json(exports_[0]);
      }
      return json({ detail: "not found" }, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("ExternalAccessPage", () => {
  it("says what stands between the platform and an external agent", async () => {
    platform = { ...settings, base_url: null, grants_enforced: false };
    render(<ExternalAccessPage />);
    const status = await screen.findByTestId("external-status");
    expect(status.textContent).toContain("External access is off");
    expect(status.textContent).toContain("EAP_EXTERNAL_BASE_URL");
    expect(status.textContent).toContain("EAP_AGENT_GRANTS_ENFORCE");
    expect(await screen.findByTestId("external-clients-empty")).toBeTruthy();
    expect(screen.getByTestId("external-exports-empty")).toBeTruthy();
  });

  it("shows the MCP endpoint external agents connect to, once the public origin is set", async () => {
    render(<ExternalAccessPage />);
    const status = await screen.findByTestId("external-status");
    expect(status.textContent).toContain(`${settings.base_url}/api/v1/external/mcp`);
    expect(status.textContent).not.toContain("no public endpoint");
  });

  it("creates a client, then a run key shown once with its workflow allowlist and expiry", async () => {
    render(<ExternalAccessPage />);
    fireEvent.click(await screen.findByTestId("external-client-create"));
    fireEvent.change(await screen.findByTestId("external-client-name"), { target: { value: "codex" } });
    fireEvent.click(screen.getByTestId("external-client-submit"));
    fireEvent.click(await screen.findByRole("button", { name: "codex" }));
    fireEvent.change(await screen.findByTestId("external-key-name"), { target: { value: "laptop" } });
    // no workflow chosen: nothing to send
    expect((screen.getByTestId("external-key-submit") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Report" }));
    fireEvent.click(screen.getByTestId("external-key-submit"));
    const shown = await screen.findByTestId("external-key-secret");
    expect(within(shown).getByDisplayValue(KEY)).toBeTruthy();
    const post = calls.find((c) => c.url.endsWith("/clients/c1/keys"));
    expect(post?.body).toMatchObject({ name: "laptop", workflow_ids: ["wf-1"] });
    const days = (Date.parse((post?.body as { expires_at: string }).expires_at) - Date.now()) / 86_400_000;
    expect(days).toBeGreaterThan(89);
    expect(days).toBeLessThanOrEqual(90);
    fireEvent.click(screen.getByTestId("external-key-done"));
    await waitFor(() => expect(screen.queryByTestId("external-key-secret")).toBeNull());
    expect(document.body.textContent).not.toContain(KEY);
    // the list comes with the client reload, which can finish after the new key is shown
    expect((await screen.findByTestId("external-keys")).textContent).toContain("pos_k1_");
  });

  it("disables a client with its revision and cancels runs only on the explicit action", async () => {
    clients = [client()];
    render(<ExternalAccessPage />);
    fireEvent.click(await screen.findByRole("button", { name: "claude-code" }));
    fireEvent.click(await screen.findByTestId("external-client-toggle"));
    await waitFor(() => expect(screen.getByTestId("external-client-status").textContent).toBe("Disabled"));
    const change = calls.find((c) => c.method === "PATCH");
    expect(change?.body).toEqual({ enabled: false });
    expect(change?.ifMatch).toBe('"3"');
    expect(calls.some((c) => c.url.endsWith("/cancel-runs"))).toBe(false);
    fireEvent.click(screen.getByTestId("external-cancel-runs"));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel runs" }));
    await waitFor(() => expect(screen.getByTestId("external-cancel-result").textContent).toContain("2 runs cancelled"));
  });

  it("creates an export disabled", async () => {
    render(<ExternalAccessPage />);
    fireEvent.click(await screen.findByTestId("external-export-create"));
    fireEvent.change(await screen.findByTestId("external-export-name"), { target: { value: "run_report" } });
    fireEvent.change(screen.getByTestId("external-export-title"), { target: { value: "Run the report" } });
    await waitFor(() => expect((screen.getByTestId("external-export-workflow") as HTMLSelectElement).value).toBe("wf-1"));
    fireEvent.click(screen.getByTestId("external-export-submit"));
    await waitFor(() => expect(screen.getByTestId("external-export-status").textContent).toBe("Disabled"));
    const post = calls.find((c) => c.method === "POST");
    expect(post?.body).toEqual({ name: "run_report", title: "Run the report", description: "", workflow_id: "wf-1", version: "latest", protocols: ["mcp"], result: "output", max_runtime_seconds: 3600 });
  });

  it("shows the enable card and enables with the hash the person saw", async () => {
    exports_ = [exported()];
    render(<ExternalAccessPage />);
    fireEvent.click(await screen.findByRole("button", { name: "run_report" }));
    const card = await screen.findByTestId("external-enable-card");
    expect(card.textContent).toContain("Report");
    expect(card.textContent).toContain("version 2");
    expect(within(card).getByTestId("external-input-schema").textContent).toContain('"required"');
    expect(within(card).getByTestId("external-warnings").textContent).toContain("side effects");
    expect(within(card).getByTestId("external-reach").textContent).toContain("claude-code");
    expect(card.textContent).toContain("return value leaves the platform");
    expect(within(card).getByTestId("external-hash").textContent).toContain(HASH.slice(0, 16));
    expect(screen.getByTestId("external-disabled-reason").textContent).toContain("changed the input or the side effects");
    fireEvent.click(screen.getByTestId("external-enable"));
    fireEvent.click(await screen.findByRole("button", { name: "Enable" }));
    await waitFor(() => expect(screen.getByTestId("external-export-status").textContent).toBe("Enabled"));
    expect(calls.find((c) => c.url.endsWith("/enable"))?.body).toEqual({ approved_hash: HASH, confirm: true });
  });

  it("does not offer to enable while the platform cannot", async () => {
    platform = { ...settings, grants_enforced: false };
    exports_ = [exported()];
    render(<ExternalAccessPage />);
    fireEvent.click(await screen.findByRole("button", { name: "run_report" }));
    expect((await screen.findByTestId("external-enable") as HTMLButtonElement).disabled).toBe(true);
  });

  it("makes the A2A endpoint with what the public card says, then enables it", async () => {
    render(<ExternalAccessPage />);
    const section = await screen.findByTestId("external-a2a");
    // the section renders before its own request for the endpoint answers
    await within(section).findByTestId("external-a2a-save");
    expect(section.textContent).toContain("Anyone with the URL reads");
    expect((within(section).getByTestId("external-a2a-save") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(section).getByTestId("external-a2a-name"), { target: { value: "Acme" } });
    fireEvent.change(within(section).getByTestId("external-a2a-description"), { target: { value: "Workflows." } });
    fireEvent.click(within(section).getByTestId("external-a2a-save"));
    await waitFor(() => expect(screen.getByTestId("external-a2a-status").textContent).toBe("Disabled"));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ card_name: "Acme", card_description: "Workflows.", enabled: false });
    expect(within(section).getByDisplayValue(`https://durable.example.test/api/v1/external/a2a/${"a".repeat(32)}`)).toBeTruthy();
    fireEvent.click(screen.getByTestId("external-a2a-toggle"));
    await waitFor(() => expect(screen.getByTestId("external-a2a-status").textContent).toBe("Enabled"));
    expect(calls.filter((c) => c.method === "PUT").at(-1)?.body).toEqual({ card_name: "Acme", card_description: "Workflows.", enabled: true });
  });

  it("rotates the endpoint id only after confirming that the old URL stops working", async () => {
    endpoint = anEndpoint({ enabled: true });
    render(<ExternalAccessPage />);
    fireEvent.click(await screen.findByTestId("external-a2a-rotate"));
    expect(calls.some((c) => c.url.endsWith("/rotate"))).toBe(false);
    expect(document.body.textContent).toContain("old URL stops answering");
    fireEvent.click(await screen.findByRole("button", { name: "Rotate" }));
    await waitFor(() => expect(screen.getByDisplayValue(`https://durable.example.test/api/v1/external/a2a/${"f".repeat(32)}`)).toBeTruthy());
  });
});
