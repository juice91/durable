import { afterEach, describe, expect, it, vi } from "vitest";
import { iamAnswer, type TestRole } from "@/test/iamFixtures";
import { apiErrorFromResponse } from "./api";
import { loadCapabilities, resetCapabilitiesForTests } from "./iam";

const body = (refused: unknown, status = 403) => new Response(JSON.stringify({ detail: "not allowed", code: "forbidden", refused }), { status, headers: { "Content-Type": "application/json" } });
async function signIn(role: TestRole) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => iamAnswer(role, String(url))!));
  await loadCapabilities();
}

afterEach(() => {
  resetCapabilitiesForTests();
  vi.unstubAllGlobals();
});

describe("the message of a refused call", () => {
  it.each([
    [{ action: "run", kind: "agent", id: "a1" }, "Your role (viewer) can't run this agent. Ask an admin for the editor role or a runner grant on it."],
    [{ action: "create", kind: "api_key", id: null }, "Your role (viewer) can't create an API key. Ask an admin for the admin role."],
    [{ action: "edit", kind: "workflow", id: "w1" }, "Your role (viewer) can't edit this workflow. Ask an admin for the editor role or an owner grant on it."],
    [{ action: "archive", kind: "knowledge_base", id: "k1" }, "Your role (viewer) can't delete this knowledge base. Ask an admin for the admin role or an owner grant on it."],
  ])("names the role, the action and the kind, and what to ask for: %o", async (refused, message) => {
    await signIn("viewer");
    const error = await apiErrorFromResponse(body(refused));
    expect(error.status).toBe(403);
    expect(error.message).toBe(message);
  });

  it("still names the action and the kind when the explanation cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (String(url).includes("explain") ? new Response("{}", { status: 500 }) : iamAnswer("viewer", String(url))!)));
    await loadCapabilities();
    const error = await apiErrorFromResponse(body({ action: "run", kind: "workflow", id: "w1" }));
    expect(error.message).toBe("Your role (viewer) can't run this workflow. Ask an admin for access.");
  });

  it("leaves other refusals as the server worded them", async () => {
    await signIn("viewer");
    const response = new Response(JSON.stringify({ detail: "origin not allowed" }), { status: 403 });
    expect((await apiErrorFromResponse(response)).message).toBe("origin not allowed");
  });
});
