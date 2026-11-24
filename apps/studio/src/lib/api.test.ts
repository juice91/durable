import { describe, it, expect, beforeEach, afterAll, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { api, ApiError } from "./api";

// Mock fetch globally
const originalFetch = global.fetch;

describe("api client", () => {
  beforeEach(() => {
    delete process.env.NEXT_PUBLIC_EAP_API_URL;
    process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer dev:test-tenant:test-user";
    global.fetch = vi.fn();
  });

  afterAll(() => {
    global.fetch = originalFetch;
  });

  it("sends the auth header and base URL", async () => {
    process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer dev:test-tenant:test-user";
    const mockAgents: any[] = [{ id: "1", name: "Test", draft_spec: {} as any, latest_version: null }];
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => mockAgents,
    });

    await api.agents.list();

    expect(global.fetch).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/agents",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer dev:test-tenant:test-user",
          "Content-Type": "application/json",
        }),
      })
    );
  });

  it("sends no Authorization header without a configured credential: the Access cookie identifies the user", async () => {
    delete process.env.NEXT_PUBLIC_EAP_DEV_TOKEN;
    (global.fetch as any).mockResolvedValueOnce({ ok: true, json: async () => [] });
    await api.agents.list();
    const init = (global.fetch as any).mock.calls[0][1];
    expect(init.headers).not.toHaveProperty("Authorization");
    expect(init.headers).toEqual(expect.objectContaining({ "Content-Type": "application/json" }));
  });

  it("wraps spec in {spec} for POST /api/v1/agents", async () => {
    const spec = {
      spec_version: 1 as const,
      name: "Test Agent",
      instructions: "Test",
      model: { provider: "openai", name: "gpt-4" },
      tools: [],
    };
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "1", name: "Test Agent", draft_spec: spec, latest_version: null }),
    });

    await api.agents.create(spec);

    expect(global.fetch).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/agents",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ spec }),
      })
    );
  });

  it("wraps spec in {spec} for PATCH /api/v1/agents/{id}", async () => {
    const spec = {
      spec_version: 1 as const,
      name: "Updated Agent",
      instructions: "Updated instructions",
      model: { provider: "openai", name: "gpt-4" },
      tools: [],
    };
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "1", name: "Updated Agent", draft_spec: spec, latest_version: 1 }),
    });

    await api.agents.update("1", spec);

    expect(global.fetch).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/agents/1",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ spec }),
      })
    );
  });

  it("handles 409 conflict with detail message", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ detail: "Already published" }),
    });

    await expect(api.agents.list()).rejects.toThrow("Already published");
  });

  it("handles 422 validation errors", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: false,
      status: 422,
      json: async () => ({
        detail: [
          { msg: "Field required" },
          { msg: "Invalid value" },
        ],
      }),
    });

    await expect(api.agents.list()).rejects.toThrow("Field required, Invalid value");
  });

  it("handles 404 not found", async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({ detail: "Agent not found" }),
    });

    await expect(api.agents.get("nonexistent")).rejects.toThrow("Agent not found");
  });
});
