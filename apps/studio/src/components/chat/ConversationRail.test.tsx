import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationRail, RAIL_SETTLE_MS, RAIL_SETTLE_READS, turnUnfinished } from "./ConversationRail";
import {
  listConversations,
  renameConversation,
  type Conversation,
  type ConversationPage,
} from "@/lib/conversations";

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
  vi.clearAllMocks();
});

describe("ConversationRail", () => {
  it("shows loading, then the conversations with turn count and last status", async () => {
    vi.mocked(listConversations).mockResolvedValue(
      page([conv("c1", "Printer", { last_turn_display_status: "needs_attention" }), conv("c2", null, { turn_count: 1 })]),
    );
    renderRail({ selectedId: "c1" });
    expect(screen.getByRole("status")).toHaveTextContent("Loading conversations");
    const printer = await screen.findByRole("button", { name: /^Printer/ });
    expect(printer).toHaveTextContent("2 turns · Needs attention");
    expect(printer).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("button", { name: /^Untitled conversation/ })).toHaveTextContent("1 turn · Completed");
    expect(listConversations).toHaveBeenCalledWith({ agentId: "agent-1", limit: 30 });
  });

  it("shows an empty state, and nothing to list without an agent", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([]));
    const { rerender, onNew, onSelect } = renderRail();
    expect(await screen.findByText(/No conversations yet/)).toBeInTheDocument();
    rerender(<ConversationRail agentId="" selectedId={null} onSelect={onSelect} onNew={onNew} />);
    expect(screen.getByText(/Choose an agent/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New conversation" })).toBeDisabled();
    expect(listConversations).toHaveBeenCalledTimes(1);
  });

  it("reports a failed load", async () => {
    vi.mocked(listConversations).mockRejectedValue(new Error("down"));
    renderRail();
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to load conversations.");
  });

  it("selects and starts new conversations", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
    const { onSelect, onNew } = renderRail();
    fireEvent.click(await screen.findByRole("button", { name: /^Printer/ }));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: "c1" }));
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    expect(onNew).toHaveBeenCalledTimes(1);
  });

  it("loads the next page with the opaque cursor and does not repeat a conversation", async () => {
    vi.mocked(listConversations)
      .mockResolvedValueOnce(page([conv("c1", "One"), conv("c2", "Two")], "cursor-1"))
      .mockResolvedValueOnce(page([conv("c2", "Two"), conv("c3", "Three")], null));
    renderRail();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("button", { name: /^Three/ })).toBeInTheDocument();
    expect(listConversations).toHaveBeenLastCalledWith({ agentId: "agent-1", limit: 30, before: "cursor-1" });
    expect(screen.getAllByRole("button", { name: /^Two/ })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

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

  it("renames inline with Enter, trimming the title", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
    vi.mocked(renameConversation).mockResolvedValue(conv("c1", "Billing"));
    const { onRenamed } = renderRail();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
    const input = screen.getByRole("textbox", { name: "Conversation title" });
    expect(input).toHaveValue("Printer");
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: "  Billing  " } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(renameConversation).toHaveBeenCalledWith("c1", "Billing"));
    expect(await screen.findByRole("button", { name: /^Billing/ })).toBeInTheDocument();
    expect(onRenamed).toHaveBeenCalledWith(expect.objectContaining({ title: "Billing" }));
  });

  it("returns focus to the conversation's row after a rename is saved or cancelled (browser check)", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
    vi.mocked(renameConversation).mockResolvedValue(conv("c1", "Billing"));
    renderRail();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Conversation title" }), { key: "Escape" });
    expect(screen.getByRole("button", { name: /^Printer/ })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Rename Printer" }));
    const input = screen.getByRole("textbox", { name: "Conversation title" });
    fireEvent.change(input, { target: { value: "Billing" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByRole("button", { name: /^Billing/ })).toHaveFocus());
    expect(document.activeElement).not.toBe(document.body);
  });

  it("draws the Rename button in full-strength muted text, never faded below contrast (browser check)", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
    renderRail();
    const rename = await screen.findByRole("button", { name: "Rename Printer" });
    expect(rename).toHaveClass("text-muted-foreground");
    expect(rename.className).not.toMatch(/(^|\s)(group-hover:|focus-visible:|focus:)?opacity-/);
  });

  it("leaves focus where the user moved it when leaving the field commits the rename", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
    vi.mocked(renameConversation).mockResolvedValue(conv("c1", "Billing"));
    renderRail();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
    const input = screen.getByRole("textbox", { name: "Conversation title" });
    fireEvent.change(input, { target: { value: "Billing" } });
    const newButton = screen.getByRole("button", { name: "New conversation" });
    newButton.focus();
    fireEvent.blur(input);
    await screen.findByRole("button", { name: /^Billing/ });
    expect(newButton).toHaveFocus();
  });

  it("cancels with Escape and refuses an empty title", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
    renderRail();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
    let input = screen.getByRole("textbox", { name: "Conversation title" });
    fireEvent.change(input, { target: { value: "Something" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.getByRole("button", { name: /^Printer/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Rename Printer" }));
    input = screen.getByRole("textbox", { name: "Conversation title" });
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByRole("alert")).toHaveTextContent("1 to 200 characters");
    expect(renameConversation).not.toHaveBeenCalled();
  });

  it("keeps the field open and says so when the rename fails", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
    vi.mocked(renameConversation).mockRejectedValue(new Error("422"));
    renderRail();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
    const input = screen.getByRole("textbox", { name: "Conversation title" });
    fireEvent.change(input, { target: { value: "New" } });
    fireEvent.blur(input);
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to rename");
    expect(screen.getByRole("textbox", { name: "Conversation title" })).toBeInTheDocument();
  });

  it("moves focus between conversations with the arrow keys", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "One"), conv("c2", "Two")]));
    renderRail();
    const one = await screen.findByRole("button", { name: /^One/ });
    one.focus();
    fireEvent.keyDown(one, { key: "ArrowDown" });
    expect(screen.getByRole("button", { name: /^Two/ })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("button", { name: /^Two/ }), { key: "ArrowUp" });
    expect(one).toHaveFocus();
  });

  it("refreshes the first page when refreshKey changes", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "One")]));
    const { rerender, onSelect, onNew } = renderRail();
    await screen.findByRole("button", { name: /^One/ });
    vi.mocked(listConversations).mockResolvedValue(page([conv("c2", "Newer"), conv("c1", "One")]));
    rerender(
      <ConversationRail agentId="agent-1" selectedId={null} refreshKey={1} onSelect={onSelect} onNew={onNew} />,
    );
    expect(await screen.findByRole("button", { name: /^Newer/ })).toBeInTheDocument();
    expect(listConversations).toHaveBeenCalledTimes(2);
  });

  it("refreshes in place: no loading swap, focus and Load more pages kept", async () => {
    const first = Array.from({ length: 30 }, (_, i) => conv(`c${i}`, `Chat ${i}`));
    vi.mocked(listConversations)
      .mockResolvedValueOnce(page(first, "cursor-1"))
      .mockResolvedValueOnce(page([conv("c30", "Chat 30")], null));
    const { rerender, onSelect, onNew } = renderRail();
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("button", { name: /^Chat 30/ })).toBeInTheDocument();
    const focused = screen.getByRole("button", { name: /^Chat 5\b/ });
    focused.focus();

    let resolveRefresh: (value: ConversationPage) => void = () => {};
    vi.mocked(listConversations).mockImplementationOnce(
      () => new Promise((resolve) => (resolveRefresh = resolve)),
    );
    rerender(
      <ConversationRail agentId="agent-1" selectedId={null} refreshKey={1} onSelect={onSelect} onNew={onNew} />,
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(focused).toHaveFocus();
    expect(screen.getByRole("button", { name: /^Chat 30/ })).toBeInTheDocument();

    // A new turn moved Chat 30 to the top: the first page comes back with it and without Chat 29.
    await act(async () => resolveRefresh(page([conv("c30", "Chat 30"), ...first.slice(0, 29)], "cursor-x")));
    const names = screen
      .getAllByRole("button")
      .filter((b) => b.hasAttribute("data-conversation"))
      .map((b) => b.getAttribute("data-conversation"));
    expect(names).toEqual(["c30", ...first.slice(0, 29).map((c) => c.id), "c29"]);
    expect(focused).toBeInTheDocument();
    expect(focused).toHaveFocus();
    // All pages were loaded before the refresh, so there is still nothing more to load.
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("keeps a rename in progress across a refresh", async () => {
    vi.mocked(listConversations).mockResolvedValue(page([conv("c1", "Printer")]));
    const { rerender, onSelect, onNew } = renderRail();
    fireEvent.click(await screen.findByRole("button", { name: "Rename Printer" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Conversation title" }), { target: { value: "Typing" } });
    rerender(
      <ConversationRail agentId="agent-1" selectedId={null} refreshKey={1} onSelect={onSelect} onNew={onNew} />,
    );
    await waitFor(() => expect(listConversations).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("textbox", { name: "Conversation title" })).toHaveValue("Typing");
  });
});

describe("ConversationRail: a finished turn's status settles", () => {
  const queued = conv("c1", "Printer", { turn_count: 1, last_turn_status: "queued", last_turn_display_status: "queued" });
  const done = conv("c1", "Printer", { turn_count: 1 });

  it("turnUnfinished: queued, running and recovering", () => {
    expect(turnUnfinished(queued)).toBe(true);
    expect(turnUnfinished({ last_turn_status: "running", last_turn_display_status: "recovering" })).toBe(true);
    expect(turnUnfinished(done)).toBe(false);
    expect(turnUnfinished({ last_turn_status: null, last_turn_display_status: null })).toBe(false);
  });

  it("reads the list again while a turn shows unfinished (the end event comes before the run's status)", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(listConversations).mockResolvedValueOnce(page([queued])).mockResolvedValue(page([done]));
      renderRail();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText("1 turn · Queued")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(RAIL_SETTLE_MS);
      });
      expect(screen.getByText("1 turn · Completed")).toBeInTheDocument();
      expect(listConversations).toHaveBeenCalledTimes(2);
      // Settled: no more reads.
      for (let step = 0; step < 3; step += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(RAIL_SETTLE_MS);
        });
      }
      expect(listConversations).toHaveBeenCalledTimes(2);
      // The re-read is quiet: no loading line.
      expect(screen.queryByText(/Loading conversations/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops after RAIL_SETTLE_READS re-reads; a refresh starts the budget again", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(listConversations).mockResolvedValue(page([queued]));
      const view = renderRail({ refreshKey: 0 });
      for (let step = 0; step < RAIL_SETTLE_READS + 3; step += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(RAIL_SETTLE_MS);
        });
      }
      expect(listConversations).toHaveBeenCalledTimes(1 + RAIL_SETTLE_READS);
      view.rerender(<ConversationRail agentId="agent-1" selectedId={null} onSelect={vi.fn()} onNew={vi.fn()} refreshKey={1} />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(RAIL_SETTLE_MS);
      });
      expect(listConversations).toHaveBeenCalledTimes(1 + RAIL_SETTLE_READS + 2);
    } finally {
      vi.useRealTimers();
    }
  });
});
