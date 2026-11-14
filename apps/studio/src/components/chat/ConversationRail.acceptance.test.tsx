import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationRail } from "./ConversationRail";
import { listConversations, renameConversation, type Conversation, type ConversationPage } from "@/lib/conversations";

vi.mock("@/lib/conversations", () => ({
  listConversations: vi.fn(),
  renameConversation: vi.fn(),
}));

const conv = (id: string, title: string | null, extra: Partial<Conversation> = {}): Conversation => ({
  id,
  agent_id: "agent-1",
  title,
  turn_count: 2,
  last_turn_run_id: "run",
  last_turn_status: "completed",
  last_turn_display_status: "completed",
  created_at: "2026-11-12T00:00:00Z",
  updated_at: "2026-11-12T00:00:00Z",
  ...extra,
});

const page = (items: Conversation[], next_before: string | null = null): ConversationPage => ({
  items,
  next_before,
});

function renderRail(props: Partial<React.ComponentProps<typeof ConversationRail>> = {}) {
  const onSelect = vi.fn();
  const onNew = vi.fn();
  const onRenamed = vi.fn();
  const view = render(
    <ConversationRail
      agentId="agent-1"
      selectedId={null}
      onSelect={onSelect}
      onNew={onNew}
      onRenamed={onRenamed}
      {...props}
    />,
  );
  return { ...view, onSelect, onNew, onRenamed };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(listConversations).mockResolvedValue(page([]));
});

describe("ConversationRail", () => {
  describe("paging and keyboard navigation", () => {
    it("loads multiple pages and does not repeat conversations", async () => {
      vi.mocked(listConversations)
        .mockResolvedValueOnce(page([conv("c1", "First"), conv("c2", "Second")], "cursor-1"))
        .mockResolvedValueOnce(page([conv("c2", "Second"), conv("c3", "Third")], "cursor-2"))
        .mockResolvedValueOnce(page([conv("c3", "Third"), conv("c4", "Fourth")], null));
      renderRail();
      const loadMore = await screen.findByRole("button", { name: "Load more" });
      fireEvent.click(loadMore);
      await waitFor(() => expect(listConversations).toHaveBeenLastCalledWith({ agentId: "agent-1", limit: 30, before: "cursor-1" }));
      fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
      await waitFor(() => expect(listConversations).toHaveBeenLastCalledWith({ agentId: "agent-1", limit: 30, before: "cursor-2" }));
      await waitFor(() => expect(screen.getAllByRole("button", { name: /^Fourth/ })).toHaveLength(1));
      expect(screen.getAllByRole("button", { name: /^Second/ })).toHaveLength(1);
      expect(screen.getAllByRole("button", { name: /^Third/ })).toHaveLength(1);
      expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    });

    it("moves focus between conversations with arrow keys", async () => {
      vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "One"), conv("c2", "Two"), conv("c3", "Three")]));
      renderRail();
      const one = await screen.findByRole("button", { name: /^One/ });
      one.focus();
      fireEvent.keyDown(one, { key: "ArrowDown" });
      expect(screen.getByRole("button", { name: /^Two/ })).toHaveFocus();
      fireEvent.keyDown(screen.getByRole("button", { name: /^Two/ }), { key: "ArrowDown" });
      expect(screen.getByRole("button", { name: /^Three/ })).toHaveFocus();
      fireEvent.keyDown(screen.getByRole("button", { name: /^Three/ }), { key: "ArrowUp" });
      expect(screen.getByRole("button", { name: /^Two/ })).toHaveFocus();
    });
  });

  describe("rename behavior", () => {
    it("renames inline with Enter, trimming the title", async () => {
      vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "  Printer  ")]));
      vi.mocked(renameConversation).mockResolvedValue(conv("c1", "Billing"));
      const { onRenamed } = renderRail();
      fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
      const input = screen.getByRole("textbox", { name: "Conversation title" });
      fireEvent.change(input, { target: { value: "  Billing  " } });
      fireEvent.keyDown(input, { key: "Enter" });
      await waitFor(() => expect(renameConversation).toHaveBeenCalledWith("c1", "Billing"));
      expect(await screen.findByRole("button", { name: /^Billing/ })).toBeInTheDocument();
      expect(onRenamed).toHaveBeenCalledWith(expect.objectContaining({ title: "Billing" }));
    });

    it("cancels rename with Escape", async () => {
      vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
      renderRail();
      fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
      const input = screen.getByRole("textbox", { name: "Conversation title" });
      fireEvent.change(input, { target: { value: "Something" } });
      fireEvent.keyDown(input, { key: "Escape" });
      expect(screen.getByRole("button", { name: /^Printer/ })).toBeInTheDocument();
      expect(screen.queryByRole("textbox", { name: "Conversation title" })).not.toBeInTheDocument();
    });

    it("blurs saves the rename", async () => {
      vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
      vi.mocked(renameConversation).mockResolvedValue(conv("c1", "Billing"));
      renderRail();
      fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
      const input = screen.getByRole("textbox", { name: "Conversation title" });
      fireEvent.change(input, { target: { value: "Billing" } });
      input.blur();
      await waitFor(() => expect(renameConversation).toHaveBeenCalledWith("c1", "Billing"));
      expect(await screen.findByRole("button", { name: /^Billing/ })).toBeInTheDocument();
    });

    it("refuses an empty or whitespace-only title", async () => {
      vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
      renderRail();
      fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
      const input = screen.getByRole("textbox", { name: "Conversation title" });
      fireEvent.change(input, { target: { value: "   " } });
      fireEvent.keyDown(input, { key: "Enter" });
      expect(await screen.findByRole("alert")).toHaveTextContent("1 to 200 characters");
      expect(renameConversation).not.toHaveBeenCalled();
    });

    it("keeps the rename field open when the API fails", async () => {
      vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
      vi.mocked(renameConversation).mockRejectedValue(new Error("Network error"));
      renderRail();
      fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
      const input = screen.getByRole("textbox", { name: "Conversation title" });
      fireEvent.change(input, { target: { value: "New" } });
      input.blur();
      expect(await screen.findByRole("alert")).toHaveTextContent("Failed to rename");
      expect(screen.getByRole("textbox", { name: "Conversation title" })).toBeInTheDocument();
    });
  });

  describe("stale response handling", () => {
    it("drops a response for an agent that is no longer selected", async () => {
      let resolveFirst: (value: ConversationPage) => void = () => {};
      vi.mocked(listConversations)
        .mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
        .mockResolvedValueOnce(page([conv("c9", "Other agent's")]));
      const { rerender, onSelect, onNew } = renderRail();
      rerender(<ConversationRail agentId="agent-2" selectedId={null} onSelect={onSelect} onNew={onNew} />);
      expect(await screen.findByRole("button", { name: /^Other agent's/ })).toBeInTheDocument();
      await act(async () => resolveFirst(page([conv("c1", "Stale")])));
      expect(screen.queryByRole("button", { name: /^Stale/ })).not.toBeInTheDocument();
    });
  });

  describe("empty states", () => {
    it("shows an empty state with no conversations", async () => {
      vi.mocked(listConversations).mockResolvedValue(page([]));
      renderRail();
      expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
    });

    it("shows nothing to list without an agent", async () => {
      const { rerender, onSelect, onNew } = renderRail();
      rerender(<ConversationRail agentId="" selectedId={null} onSelect={onSelect} onNew={onNew} />);
      expect(screen.getByText(/Choose an agent/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "New conversation" })).toBeDisabled();
    });
  });
});
