import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { listRuns, ApiError } from "./runs";

describe("listRuns", () => {
  beforeEach(() => {
    delete process.env.NEXT_PUBLIC_EAP_API_URL;
    delete process.env.NEXT_PUBLIC_EAP_DEV_TOKEN;
    process.env.NEXT_PUBLIC_EAP_API_URL = "http://localhost:8000";
    process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer dev:acme:alice";
  });

  afterEach(() => {
    delete process.env.NEXT_PUBLIC_EAP_API_URL;
    delete process.env.NEXT_PUBLIC_EAP_DEV_TOKEN;
    vi.unstubAllGlobals();
  });

  it("calls the /api/v1/runs endpoint with default parameters", async () => {
    const mockResponse = {
      items: [
        {
          run_id: "run-1",
          agent_id: "agent-1",
          version: 1,
          status: "completed",
          display_status: "completed",
          input: "test",
          output: "result",
          error: null,
          heartbeat_at: null,
          created_at: "2026-11-12T00:00:00Z",
          updated_at: "2026-11-12T00:00:01Z",
          trace_id: "trace-1",
          conversation_id: "conv-1",
          turn: 1,
        },
      ],
      next_before: null,
    };

    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(mockResponse), { status: 200 })));

    await listRuns();

    expect(fetch).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/runs?limit=50",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer dev:acme:alice",
        }),
      })
    );
  });

  it("builds URL with all query parameters", async () => {
    const mockResponse = { items: [], next_before: null };

    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(mockResponse), { status: 200 })));

    await listRuns({ limit: 25, before: "abc123", agentId: "agent-123", status: "running" });

    const calledUrl = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(calledUrl).toBe("http://localhost:8000/api/v1/runs?limit=25&before=abc123&agent_id=agent-123&status=running");
  });

  it("URL-encodes the before cursor parameter with ISO timestamp", async () => {
    const mockResponse = { items: [], next_before: null };

    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(mockResponse), { status: 200 })));

    const isoTimestamp = "2026-11-12T10:30:00+00:00";
    await listRuns({ before: isoTimestamp });

    const calledUrl = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(calledUrl).toBe("http://localhost:8000/api/v1/runs?limit=50&before=2026-11-12T10%3A30%3A00%2B00%3A00");
  });

  it("parses the response with items and next_before", async () => {
    const mockResponse = {
      items: [
        {
          run_id: "run-1",
          agent_id: "agent-1",
          version: 1,
          status: "completed",
          display_status: "completed",
          input: "test",
          output: "result",
          error: null,
          heartbeat_at: null,
          created_at: "2026-11-12T00:00:00Z",
          updated_at: "2026-11-12T00:00:01Z",
          trace_id: "trace-1",
          conversation_id: "conv-1",
          turn: 1,
        },
      ],
      next_before: "next-cursor",
    };

    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(mockResponse), { status: 200 })));

    const result = await listRuns();

    expect(result.items).toHaveLength(1);
    expect(result.items[0].run_id).toBe("run-1");
    expect(result.next_before).toBe("next-cursor");
  });

  it("handles empty response with null next_before", async () => {
    const mockResponse = { items: [], next_before: null };

    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(mockResponse), { status: 200 })));

    const result = await listRuns();

    expect(result.items).toEqual([]);
    expect(result.next_before).toBeNull();
  });

  it("throws ApiError on 404 response", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Not found" }), { status: 404 })));

    await expect(listRuns()).rejects.toBeInstanceOf(ApiError);
    await expect(listRuns()).rejects.toHaveProperty("status", 404);
    await expect(listRuns()).rejects.toHaveProperty("message", "Not found");
  });

  it("handles 422 validation errors with array detail", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: [{ msg: "Invalid limit" }] }), { status: 422 })));

    await expect(listRuns()).rejects.toThrow("Invalid limit");
    await expect(listRuns()).rejects.toHaveProperty("status", 422);
  });
});
