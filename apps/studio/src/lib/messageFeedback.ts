import { BASE_URL, apiErrorFromResponse, apiHeaders } from "@/lib/api";

// Thumbs up/down on an assistant message (W2.4): PUT sets (and replaces), DELETE clears. The
// thread GET returns the caller's own ratings as `feedback`.

export type FeedbackRating = "up" | "down";

export interface MessageFeedbackEntry {
  rating: FeedbackRating;
  comment: string | null;
}

export type FeedbackMap = Record<string, MessageFeedbackEntry>;

function url(conversationId: string, messageId: string): string {
  return `${BASE_URL}/api/v1/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/feedback`;
}

export async function setMessageFeedback(
  conversationId: string,
  messageId: string,
  rating: FeedbackRating,
  comment: string | null,
): Promise<MessageFeedbackEntry> {
  const response = await fetch(url(conversationId, messageId), {
    method: "PUT",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ rating, comment }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  const body = (await response.json()) as { rating: FeedbackRating; comment: string | null };
  return { rating: body.rating, comment: body.comment ?? null };
}

export async function clearMessageFeedback(conversationId: string, messageId: string): Promise<void> {
  const response = await fetch(url(conversationId, messageId), { method: "DELETE", headers: apiHeaders() });
  if (!response.ok && response.status !== 404) throw await apiErrorFromResponse(response);
}
