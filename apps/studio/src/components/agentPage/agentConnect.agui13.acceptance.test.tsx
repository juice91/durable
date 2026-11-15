/** AGUI.13 acceptance: the agent page header opens the Connect (AG-UI) Sheet for that agent. */
import React, { Suspense } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { get: vi.fn(), list: vi.fn(), update: vi.fn() }, versions: { list: vi.fn(), create: vi.fn() } } };
});
vi.mock("@/lib/conversations", () => ({ createConversation: vi.fn(), getConversation: vi.fn(), listConversations: vi.fn(async () => ({ items: [], next_before: null })), renameConversation: vi.fn() }));
vi.mock("@/lib/metrics", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/metrics")>()), fetchModels: vi.fn(async () => []) }));

import { api, type Agent, type AgentSpec } from "@/lib/api";
import { FrameHarness, mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH } from "@/components/frame/frameTesting";
import { nextRouter } from "@/test/nextRouterMock";
import { AgentLayout } from "./AgentLayout";

const spec = { spec_version: 1, name: "Support bot", instructions: "x", model: { provider: "test", name: "echo" }, tools: [] } as unknown as AgentSpec;
let restoreWidth = () => {};
let media: ReturnType<typeof mockMatchMedia>;
beforeEach(() => {
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  media = mockMatchMedia(true);
  const agent = { id: "a1", name: "Support bot", draft_spec: spec, latest_version: 1 } as unknown as Agent;
  vi.mocked(api.agents.get).mockResolvedValue(agent);
  vi.mocked(api.agents.list).mockResolvedValue([agent]);
  vi.mocked(api.versions.list).mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  media.restore();
  restoreWidth();
});

describe("agent page entry point", () => {
  it("Connect (AG-UI) in the header opens a Sheet with this agent's endpoint", async () => {
    nextRouter.reset(["/agents/a1/chat"]);
    render(<Suspense><FrameHarness><AgentLayout agentId="a1"><div /></AgentLayout></FrameHarness></Suspense>);
    fireEvent.click(await screen.findByTestId("connect-agui-open"));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe("/agents/a1/chat?connect=ag-ui");
    const sheet = await screen.findByTestId("connect-agui-sheet");
    expect(within(sheet).getByDisplayValue(/\/api\/v1\/ag-ui\/agents\/a1$/)).toBeInTheDocument();
    expect(sheet.innerHTML).not.toMatch(/pos_[0-9a-f]/);
  });
});
