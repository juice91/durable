// Starting a conversation turn with run options (POST /conversations/{id}/runs, P0.8 body). Unlike
// lib/conversations.startTurn, a refusal keeps the raw `detail`, so a 422 can be shown next to the
// field, model or version it is about (runOptions.classifyTurnError).
import { BASE_URL, apiHeaders } from "@/lib/api";
import type { StartedTurn } from "@/lib/conversations";
import { turnBody, type TurnOptions } from "./runOptions";

/** A turn start answered with the run's options as recorded (P0.8). */
export interface StartedTurnWithOptions extends Omit<StartedTurn, "version"> {
  /** null for a draft test turn. */
  version: number | null;
  draft?: boolean;
  model_id?: string | null;
  model_overridden?: boolean;
}

/** A refused turn start: the HTTP status and the response's `detail` (a string or a 422 list). */
export class TurnStartError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item) => String((item as { msg?: unknown })?.msg ?? "")).join(", ")
          : `HTTP ${status}`,
    );
    this.name = "TurnStartError";
  }
}

/**
 * Start the next turn with its options. Idempotent on `runId` when the same input and options are
 * sent again (a reload re-posts a recorded turn with `optionsFromRun`); 409 while the previous turn
 * runs or when the run id was used with different options.
 */
export async function startTurnWithOptions(
  conversationId: string,
  runId: string,
  input: string,
  options: TurnOptions,
): Promise<StartedTurnWithOptions> {
  const response = await fetch(
    `${BASE_URL}/api/v1/conversations/${encodeURIComponent(conversationId)}/runs`,
    {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(turnBody(runId, input, options)),
    },
  );
  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = (await response.json())?.detail ?? null;
    } catch {
      // Not JSON: the status is all there is.
    }
    throw new TurnStartError(response.status, detail);
  }
  return response.json();
}
