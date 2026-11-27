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

import { WebhooksPage } from "./WebhooksPage";
import type { WebhookTrigger } from "@/lib/webhooks";

const SECRET = "generated-signing-secret-value-0123456789";
const HASH = "a".repeat(64);

function trigger(over: Partial<WebhookTrigger> = {}): WebhookTrigger {
  return {
    id: "0f3c2a5e-1111-4222-8333-444455556666",
    name: "github-push",
    description: "",
    workflow_id: "wf-1",
    workflow_version: 1,
    version: 1,
    preset: "github",
    preset_config: {},
    secrets: [{ ref: "secret:hook", added_at: null, last_matched_at: null }],
    tolerance_seconds: 300,
    actions: { start: { payload_schema: { type: "object" }, input_map: { repo: { $pointer: "/repository/full_name" } }, run_name: null } },
    limits: { rate_per_minute: 60, max_concurrent_runs: 5, max_runs_per_day: 2000, max_body_bytes: 262144 },
    status: "disabled",
    config_hash: HASH,
    approved: false,
    enable_requested_by: null,
    enable_requested_at: null,
    enabled_by: null,
    enabled_at: null,
    last_delivery_at: null,
    last_outcome: null,
    revision: 1,
    created_by: "user:alice",
    created_at: "2026-11-23T10:00:00Z",
    updated_at: "2026-11-23T10:00:00Z",
    url: "https://hooks.example.test/api/v1/hooks/0f3c2a5e-1111-4222-8333-444455556666",
    warnings: [{ code: "webhook_version_latest", message: "follows latest" }],
    ...over,
  };
}

let stored: WebhookTrigger[];
let calls: Array<{ url: string; method: string; body: unknown }>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
  stored = [];
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit = {}) => {
      const url = String(input);
      const method = init.method ?? "GET";
      calls.push({ url, method, body: init.body ? JSON.parse(String(init.body)) : undefined });
      if (url.endsWith("/api/v1/workflows")) return json([{ id: "wf-1", name: "Repo push", archived: false }]);
      if (url.endsWith("/api/v1/webhook-triggers") && method === "GET") return json({ items: stored });
      if (url.endsWith("/api/v1/webhook-triggers") && method === "POST") {
        const body = JSON.parse(String(init.body));
        const row = trigger({ name: body.name });
        stored = [row, ...stored];
        return json({ ...row, generated_secret: body.generate ? SECRET : undefined }, 201);
      }
      const enable = /webhook-triggers\/([^/]+)\/enable$/.exec(url);
      if (enable && method === "POST") {
        stored = stored.map((t) => ({ ...t, status: "enabled", approved: true, enabled_by: "user:alice", enabled_at: "2026-11-23T11:00:00Z" }));
        return json(stored[0]);
      }
      if (/webhook-triggers\/[^/]+\/events/.test(url)) return json({ items: [{ id: "e1", at: "2026-11-23T10:00:00Z", actor: "user:alice", kind: "created", detail: {} }] });
      if (/webhook-deliveries/.test(url)) return json({ items: [] });
      if (/webhook-triggers\/[^/]+$/.test(url) && method === "GET") return json({ ...stored[0], refusals_since_start: { signature_mismatch: 2 } });
      return json({ detail: "not found" }, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("WebhooksPage", () => {
  it("shows the empty state and creates a trigger with a generated secret shown once", async () => {
    render(<WebhooksPage />);
    expect(await screen.findByTestId("webhooks-empty")).toBeTruthy();
    fireEvent.click(screen.getByTestId("webhook-create"));
    fireEvent.change(await screen.findByTestId("webhook-name"), { target: { value: "github-push" } });
    await waitFor(() => expect((screen.getByTestId("webhook-workflow") as HTMLSelectElement).value).toBe("wf-1"));
    fireEvent.click(screen.getByTestId("webhook-create-submit"));
    const secret = await screen.findByTestId("webhook-secret");
    expect((within(secret).getByDisplayValue(SECRET) as HTMLInputElement).value).toBe(SECRET);
    const post = calls.find((c) => c.method === "POST");
    expect(post?.body).toMatchObject({ name: "github-push", workflow_id: "wf-1", version: "latest", preset: "github", generate: true });
    // closing drops the secret: the list never shows it
    fireEvent.click(screen.getByTestId("webhook-done"));
    await waitFor(() => expect(screen.queryByTestId("webhook-secret")).toBeNull());
    expect(await screen.findByText("github-push")).toBeTruthy();
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("refuses a secret value typed into the secret name and invalid JSON", async () => {
    render(<WebhooksPage />);
    fireEvent.click(await screen.findByTestId("webhook-create"));
    fireEvent.change(await screen.findByTestId("webhook-name"), { target: { value: "x" } });
    fireEvent.click(screen.getByTestId("webhook-secret-existing"));
    fireEvent.change(screen.getByTestId("webhook-secret-name"), { target: { value: "Sentinel Value With Spaces!" } });
    fireEvent.click(screen.getByTestId("webhook-create-submit"));
    expect((await screen.findByTestId("webhook-create-error")).textContent).toContain("name of an existing secret");
    fireEvent.change(screen.getByTestId("webhook-secret-name"), { target: { value: "hook" } });
    fireEvent.change(screen.getByTestId("webhook-schema"), { target: { value: "{not json" } });
    fireEvent.click(screen.getByTestId("webhook-create-submit"));
    expect((await screen.findByTestId("webhook-create-error")).textContent).toContain("not valid JSON");
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("shows the enable card with the warnings and enables with the config hash the person saw", async () => {
    stored = [trigger()];
    render(<WebhooksPage />);
    fireEvent.click(await screen.findByText("github-push"));
    const card = await screen.findByTestId("webhook-enable-card");
    expect(within(card).getByTestId("webhook-warnings").textContent).toContain("follows latest");
    expect(within(card).getByTestId("webhook-hash").textContent).toContain(HASH.slice(0, 16));
    fireEvent.click(screen.getByTestId("webhook-enable"));
    fireEvent.click(await screen.findByRole("button", { name: "Enable" }));
    await waitFor(() => expect(screen.getByTestId("webhook-status").textContent).toBe("Enabled"));
    const enable = calls.find((c) => c.url.endsWith("/enable"));
    expect(enable?.body).toEqual({ config_hash: HASH, confirm: true });
    expect(screen.getByTestId("webhook-refusals").textContent).toContain("signature_mismatch 2");
  });
});
