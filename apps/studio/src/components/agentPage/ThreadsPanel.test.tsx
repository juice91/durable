import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ThreadsPanel } from "./ThreadsPanel";
import { cloneConversation, listConversations, type Conversation } from "@/lib/conversations";

vi.mock("@/lib/conversations", () => ({
  cloneConversation: vi.fn(),
  listConversations: vi.fn(),
  renameConversation: vi.fn(),
  deleteConversation: vi.fn(),
}));

const conv = (id: string, title: string): Conversation => ({
  id,
  agent_id: "a1",
  title,
  turn_count: 1,
  last_turn_run_id: "r",
  last_turn_status: "completed",
  last_turn_display_status: "completed",
  created_at: "2026-11-12T00:00:00Z",
  updated_at: "2026-11-12T00:00:00Z",
});

beforeEach(() => vi.clearAllMocks());

describe("ThreadsPanel clone", () => {
  it("opens the cloned conversation through onSelect", async () => {
    vi.mocked(listConversations).mockResolvedValue({ items: [conv("c1", "Printer")], next_before: null });
    vi.mocked(cloneConversation).mockResolvedValue(conv("c2", "Printer (copy)"));
    const onSelect = vi.fn();
    render(
      <ThreadsPanel agentId="a1" selectedId="c1" refreshKey={0} onSelect={onSelect} onNew={vi.fn()} memorySpec={null} />,
    );
    const trigger = await screen.findByTestId("conversation-actions");
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(await screen.findByTestId("conversation-clone"));
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: "c2" })));
    expect(cloneConversation).toHaveBeenCalledWith("c1");
  });
});
