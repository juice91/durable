// A refused chat send says why (not "not allowed"), over both chat protocols; a refused role gets no composer.
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/agents/arch/chat",
  useParams: () => ({ id: "arch" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.unmock("@/lib/permissions");

import { ChatTab } from "@/components/agentPage/ChatTab";
import { CHAT_PROTOCOL_STORAGE_KEY } from "@/components/chat/chatProtocol";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { resetCapabilitiesForTests } from "@/lib/iam";
import { iamAnswer } from "./iamFixtures";
import { json, settle } from "./roleHarness";

const spec = { spec_version: 1, name: "Architect", instructions: "Design.", model: { provider: "test", name: "echo" }, tools: [] };
const ARCHITECT = { id: "arch", name: "Architect", builtin: true, builtin_role: "architect", latest_version: 1, draft_spec: spec };
const SUPPORT = { id: "a1", name: "Support bot", latest_version: 1, draft_spec: spec };
const SENTENCE = "Your role (viewer) can't run this agent. Ask an admin for the editor role or a runner grant on it.";

function stub() {
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const iam = iamAnswer("viewer", url.toString());
    if (iam) return iam;
    if (init?.method === "POST") return json({ detail: "not allowed", code: "forbidden", refused: { action: "run", kind: "agent", id: "arch" } }, 403);
    const path = url.pathname.replace(/^.*\/api\/v1/, "");
    if (path === "/agents/arch") return json(ARCHITECT);
    if (path === "/agents/a1") return json(SUPPORT);
    if (path.endsWith("/versions")) return json([]);
    if (path === "/models") return json({ items: [] });
    if (path.endsWith("/request-context")) return json({ agent_id: "arch", version: 1, draft: false, fields: [], schema: { type: "object", properties: {} } });
    return json({ items: [], next_before: null, runs: [], messages: [] });
  }));
}

const chat = (agentId: string) => (
  <TooltipProvider>
    <ChatTab agentId={agentId} />
  </TooltipProvider>
);

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  resetCapabilitiesForTests();
  vi.unstubAllGlobals();
});

describe.each(["native", "agui"])("a refused send over %s", (protocol) => {
  it("explains who, what and whom to ask", async () => {
    window.localStorage.setItem(CHAT_PROTOCOL_STORAGE_KEY, protocol);
    stub();
    render(chat("arch"));
    // The capabilities are loaded by the frame in the app; here the composer's own questions load them.
    const box = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(box).not.toBeDisabled());
    await settle();
    fireEvent.change(box, { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(screen.getAllByRole("alert").map((a) => a.textContent).join(" ")).toContain(`Failed to send the message. ${SENTENCE}`));
    expect(screen.queryByText(/not allowed/)).toBeNull();
  });
});

describe("someone who may not run the agent", () => {
  it("reads the chat, sees why, and has no composer", async () => {
    stub();
    render(chat("a1"));
    expect(await screen.findByTestId("chat-read-only")).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("why-not").textContent).toContain(SENTENCE));
    expect(screen.queryByRole("textbox", { name: "Message" })).toBeNull();
  });
});
