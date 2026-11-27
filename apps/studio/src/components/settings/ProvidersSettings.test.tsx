import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";
import type { ProviderStatus } from "@/lib/providers";
import { ProvidersSettings } from "./ProvidersSettings";

const row = (over: Partial<ProviderStatus>): ProviderStatus => ({
  id: "litellm", label: "LiteLLM gateway", configured: true, env_var: "EAP_LITELLM_API_KEY", base_url_host: "gateway.example.test:4000",
  status: "ok", model_count: 12, checked_at: "2026-11-16T10:00:00Z", error: null, ...over,
});
let providers: ProviderStatus[];
let calls: { method: string; path: string }[];
let failTest = false;

beforeEach(() => {
  calls = [];
  failTest = false;
  providers = [
    row({}),
    row({ id: "anthropic", label: "Anthropic", env_var: "ANTHROPIC_API_KEY", base_url_host: "api.anthropic.com", status: "unreachable", model_count: 0, error: "the provider rejected the server's key" }),
    row({ id: "openai", label: "OpenAI", env_var: "OPENAI_API_KEY", base_url_host: "api.openai.com", configured: false, status: "not_configured", model_count: 0, checked_at: null, error: "no API key is set on the server" }),
  ];
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const path = new URL(String(input), "http://localhost").pathname.replace("/api/v1", "");
    const method = init?.method ?? "GET";
    calls.push({ method, path });
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
    if (method === "GET" && path === "/providers") return json({ items: providers });
    const m = /^\/providers\/([^/]+)\/test$/.exec(path);
    if (method === "POST" && m) {
      if (failTest) return json({ detail: "boom" }, 500);
      return json(row({ id: m[1], label: "LiteLLM gateway", model_count: 99, checked_at: "2026-11-16T11:00:00Z" }));
    }
    return json({}, 404);
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Settings → Providers", () => {
  it("lists each provider with status, model count, key variable name and base URL host", async () => {
    render(<ProvidersSettings />);
    const rows = await screen.findAllByTestId("provider-row");
    expect(rows.map((r) => r.getAttribute("data-provider"))).toEqual(["litellm", "anthropic", "openai"]);
    const [litellm, anthropic, openai] = rows;
    expect(within(litellm).getByTestId("provider-status")).toHaveTextContent("Connected");
    expect(within(litellm).getByTestId("provider-model-count")).toHaveTextContent("12");
    expect(within(litellm).getByTestId("provider-env-var")).toHaveTextContent("EAP_LITELLM_API_KEY");
    expect(within(litellm).getByTestId("provider-host")).toHaveTextContent("gateway.example.test:4000");
    expect(within(anthropic).getByTestId("provider-status")).toHaveTextContent("Unreachable");
    expect(within(anthropic).getByTestId("provider-error")).toHaveTextContent("rejected the server's key");
    expect(within(openai).getByTestId("provider-status")).toHaveTextContent("Not configured");
  });

  it("says how to add a provider while none is configured, and only then", async () => {
    render(<ProvidersSettings />);
    await screen.findAllByTestId("provider-row");
    expect(screen.queryByTestId("providers-none-configured")).toBeNull();
    cleanup();
    providers = providers.map((p) => ({ ...p, configured: false, status: "not_configured" as const, model_count: 0 }));
    render(<ProvidersSettings />);
    const hint = await screen.findByTestId("providers-none-configured");
    expect(hint).toHaveTextContent(/No model provider is configured/);
    expect(hint).toHaveTextContent(/\.env.*docker compose up -d/);
  });

  it("says plainly that it is read-only and that keys come from the server", async () => {
    render(<ProvidersSettings />);
    expect(await screen.findByTestId("providers-readonly-note")).toHaveTextContent(/Read-only.*server's environment/i);
    expect(screen.queryByRole("textbox")).toBeNull(); // nothing to type a key into
  });

  it("Test connection posts to the provider's test route and shows the fresh result", async () => {
    render(<ProvidersSettings />);
    const [litellm] = await screen.findAllByTestId("provider-row");
    fireEvent.click(within(litellm).getByRole("button", { name: "Test connection to LiteLLM gateway" }));
    await waitFor(() => expect(within(litellm).getByTestId("provider-model-count")).toHaveTextContent("99"));
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual(["/providers/litellm/test"]);
  });

  it("shows a failed test as an alert and a failed load with Retry", async () => {
    failTest = true;
    render(<ProvidersSettings />);
    const [litellm] = await screen.findAllByTestId("provider-row");
    fireEvent.click(within(litellm).getByTestId("provider-test"));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    cleanup();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "down" }), { status: 500, headers: { "content-type": "application/json" } })));
    render(<ProvidersSettings />);
    expect(await screen.findByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("fits 375 px and has no axe violations", async () => {
    const restore = setWindowWidth(375);
    try {
      const { container } = render(<ProvidersSettings />);
      await screen.findAllByTestId("provider-row");
      expect(container.firstElementChild!.className).toContain("min-w-0");
      await expectNoAxeViolations(container);
    } finally {
      restore();
    }
  }, AXE_TIMEOUT_MS);
});
