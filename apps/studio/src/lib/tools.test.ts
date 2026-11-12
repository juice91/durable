import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";
import { getTool, listTools } from "./tools";

const originalFetch = global.fetch;

const tool = {
  id: "clock.now",
  title: "Current time",
  description: "Return the current time",
  side_effect: false,
  external_idempotent: true,
  input_schema: { type: "object", properties: {} },
  allowed: true,
};

describe("tools client", () => {
  beforeEach(() => {
    process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer dev:test-tenant:test-user";
    global.fetch = vi.fn();
  });

  afterAll(() => {
    global.fetch = originalFetch;
  });

  it("parses the tool catalog response", async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(new Response(JSON.stringify([tool])));

    await expect(listTools()).resolves.toEqual([tool]);
    expect(global.fetch).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/tools",
      expect.objectContaining({ headers: expect.objectContaining({ Authorization: expect.any(String) }) }),
    );
  });

  it("fetches and parses one tool by encoded id", async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(new Response(JSON.stringify(tool)));

    await expect(getTool("tickets/create")).resolves.toEqual(tool);
    expect(global.fetch).toHaveBeenCalledWith(
      "http://localhost:8000/api/v1/tools/tickets%2Fcreate",
      expect.any(Object),
    );
  });

  it("rejects malformed tool responses", async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(new Response(JSON.stringify([{ id: "broken" }])));
    await expect(listTools()).rejects.toThrow("Invalid tool catalog response");
  });
});
