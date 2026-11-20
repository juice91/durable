// The AI explanation of a published workflow version (`GET|POST /api/v1/workflows/{id}/explanation`):
// a summary, what each input and ending means, and one plain sentence per step. It describes the
// published version and changes nothing. The language follows the browser: zh-TW for a `zh*` locale, else en.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";

export type ExplanationLocale = "zh-TW" | "en";

export interface WorkflowExplanation {
  workflow_id: string;
  version: number;
  locale: ExplanationLocale;
  model: string | null;
  created_at: string;
  summary: string;
  inputs: { name: string; meaning: string }[];
  outcomes: { status: string; meaning: string }[];
  steps: { id: string; note: string }[];
}

export const NO_MODEL_MESSAGE = "No model is available for explanations. Allow a model first (Settings, models).";

/** zh-TW when the browser's language starts with `zh`, else en. */
export function explanationLocale(language?: string | null): ExplanationLocale {
  const tag = language === undefined ? (typeof navigator === "undefined" ? "" : navigator.language) : language;
  return (tag ?? "").toLowerCase().startsWith("zh") ? "zh-TW" : "en";
}

const path = (id: string) => `${BASE_URL}/api/v1/workflows/${encodeURIComponent(id)}/explanation`;

/** The stored explanation of a version, or null when none was generated yet. */
export async function getWorkflowExplanation(
  id: string,
  version: number,
  locale: ExplanationLocale = explanationLocale(),
  signal?: AbortSignal,
): Promise<WorkflowExplanation | null> {
  const response = await fetch(`${path(id)}?version=${version}&locale=${locale}`, { headers: apiHeaders(), signal });
  if (response.status === 404) {
    const body = (await response.clone().json().catch(() => null)) as { code?: unknown } | null;
    if (body?.code === "explanation_missing") return null;
  }
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as WorkflowExplanation;
}

/** Generate (or, with a stored one and refresh false, fetch) the explanation. Takes up to 90 s. */
export async function generateWorkflowExplanation(
  id: string,
  opts: { version: number; locale?: ExplanationLocale; refresh?: boolean; signal?: AbortSignal },
): Promise<WorkflowExplanation> {
  const response = await fetch(path(id), {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ version: opts.version, locale: opts.locale ?? explanationLocale(), refresh: opts.refresh ?? false }),
    signal: opts.signal,
  });
  if (response.status === 409) {
    const body = (await response.clone().json().catch(() => null)) as { code?: unknown } | null;
    if (body?.code === "no_model") throw new ApiError(409, NO_MODEL_MESSAGE);
  }
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as WorkflowExplanation;
}

/** step id → note. */
export function stepNotes(explanation: WorkflowExplanation | null): ReadonlyMap<string, string> {
  return new Map((explanation?.steps ?? []).map((s) => [s.id, s.note]));
}

export type ExplanationStatus = "loading" | "none" | "generating" | "ready" | "error";

export interface ExplanationControl {
  status: ExplanationStatus;
  explanation: WorkflowExplanation | null;
  error: string | null;
  notes: ReadonlyMap<string, string>;
  generate: (refresh?: boolean) => void;
}

/**
 * The cached explanation of `workflowId` v`version` (null version: a draft, nothing to load) and the
 * generate action. Loading the cache never generates; only `generate` runs the helper.
 */
export function useWorkflowExplanation(workflowId: string, version: number | null): ExplanationControl {
  const [status, setStatus] = useState<ExplanationStatus>("loading");
  const [explanation, setExplanation] = useState<WorkflowExplanation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const locale = useMemo(() => explanationLocale(), []);

  useEffect(() => {
    request.current?.abort();
    setExplanation(null);
    setError(null);
    if (!workflowId || version === null) {
      setStatus("none");
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setStatus("loading");
    getWorkflowExplanation(workflowId, version, locale, controller.signal)
      .then((found) => {
        if (controller.signal.aborted) return;
        setExplanation(found);
        setStatus(found ? "ready" : "none");
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "Failed to load the explanation");
        setStatus("error");
      });
    return () => controller.abort();
  }, [workflowId, version, locale]);

  const generate = useCallback((refresh = false) => {
    if (version === null) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setStatus("generating");
    setError(null);
    generateWorkflowExplanation(workflowId, { version, locale, refresh, signal: controller.signal })
      .then((made) => {
        if (controller.signal.aborted) return;
        setExplanation(made);
        setStatus("ready");
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "Failed to generate the explanation");
        setStatus("error");
      });
  }, [workflowId, version, locale]);

  const notes = useMemo(() => stepNotes(explanation), [explanation]);
  return { status, explanation, error, notes, generate };
}
