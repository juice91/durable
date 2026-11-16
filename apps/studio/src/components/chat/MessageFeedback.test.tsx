import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";
import { FeedbackScopeContext, MessageFeedback, isRatableMessage, type FeedbackScope } from "./MessageFeedback";

const CONVERSATION = "c-1";
const MESSAGE = "r1-m0";
let fetchMock: ReturnType<typeof vi.fn>;

function stubApi(initial: Record<string, unknown> = {}) {
  fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      return new Response(JSON.stringify({ message_id: MESSAGE, ...body }), { status: 200 });
    }
    return new Response(null, { status: 204 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return initial;
}

function view(scope: Partial<FeedbackScope> = {}) {
  return render(
    <FeedbackScopeContext.Provider value={{ conversationId: CONVERSATION, initial: {}, ...scope }}>
      <MessageFeedback messageId={MESSAGE} />
    </FeedbackScopeContext.Provider>,
  );
}

beforeEach(() => {
  process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer dev:t:u";
  stubApi();
});

describe("MessageFeedback", () => {
  it("only model answers are ratable", () => {
    expect(isRatableMessage("r1-m0")).toBe(true);
    expect(isRatableMessage("r1-u")).toBe(false);
    expect(isRatableMessage("r1-t-call")).toBe(false);
  });

  it("renders nothing without a conversation", () => {
    view({ conversationId: null });
    expect(screen.queryByTestId("message-feedback")).toBeNull();
  });

  it("a thumb records the rating (PUT) and shows pressed", async () => {
    view();
    fireEvent.click(screen.getByRole("button", { name: "Good answer" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Good answer" })).toHaveAttribute("aria-pressed", "true"));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain(`/conversations/${CONVERSATION}/messages/${MESSAGE}/feedback`);
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ rating: "up", comment: null });
  });

  it("pressing the active thumb clears it (DELETE); the other thumb replaces it", async () => {
    view({ initial: { [MESSAGE]: { rating: "up", comment: null } } });
    expect(screen.getByRole("button", { name: "Good answer" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Bad answer" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Bad answer" })).toHaveAttribute("aria-pressed", "true"));
    expect(screen.getByRole("button", { name: "Good answer" })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByRole("button", { name: "Bad answer" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Bad answer" })).toHaveAttribute("aria-pressed", "false"));
    expect(fetchMock.mock.calls.at(-1)![1].method).toBe("DELETE");
  });

  it("shows the stored state and comment after a reload", () => {
    view({ initial: { [MESSAGE]: { rating: "down", comment: "too long" } } });
    expect(screen.getByRole("button", { name: "Bad answer" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("feedback-comment")).toHaveTextContent("too long");
  });

  it("a thumbs down opens a comment box that saves with the rating", async () => {
    view();
    fireEvent.click(screen.getByRole("button", { name: "Bad answer" }));
    const box = await screen.findByLabelText("Feedback comment");
    fireEvent.change(box, { target: { value: "wrong date" } });
    fireEvent.click(screen.getByRole("button", { name: "Save comment" }));
    await waitFor(() => expect(screen.getByTestId("feedback-comment")).toHaveTextContent("wrong date"));
    expect(JSON.parse(fetchMock.mock.calls.at(-1)![1].body)).toEqual({ rating: "down", comment: "wrong date" });
  });

  it("a failed save says so and keeps the previous state", async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ detail: "no" }), { status: 500 }));
    view();
    fireEvent.click(screen.getByRole("button", { name: "Good answer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save your feedback.");
    expect(screen.getByRole("button", { name: "Good answer" })).toHaveAttribute("aria-pressed", "false");
  });

  it("has no axe violations, rated or not, and fits 375 px", async () => {
    const restore = setWindowWidth(375);
    try {
      view({ initial: { [MESSAGE]: { rating: "down", comment: "x".repeat(300) } } });
      await expectNoAxeViolations(document.body);
      const row = screen.getByRole("group", { name: "Rate this answer" });
      expect(row.className).toContain("flex");
      expect(screen.getByTestId("feedback-comment").className).toContain("truncate");
    } finally {
      restore();
    }
  }, AXE_TIMEOUT_MS);
});
