"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAgUiInterrupts, useAgUiSubmitInterruptResponses, type AgUiInterrupt, type AgUiResumeEntry } from "@assistant-ui/react-ag-ui";
import { ApprovalCard } from "@/components/approvals/ApprovalCard";
import { Button } from "@/components/ui/Button";
import { toolInterruptTarget } from "./agUiChat";
import type { ToolDecision } from "./turnCapabilities";

// The AG-UI chat's tool approvals (AGUI.12, D15): each open interrupt of the run renders the shared
// ApprovalCard, and the decision goes back as a `resume` entry (Pydantic AI's payload:
// `{approved, reason?}`), which continues the same durable run. Every open interrupt must be
// answered in one resume, so decisions are collected until all are in. A decision recorded elsewhere
// meanwhile (the Approvals inbox, REST, the timeout) is reflected: the native events are re-read
// while interrupts are open, and a decided call is answered with that decision (the server keeps the
// recorded one either way and shows it).

/** Poll interval for decisions made elsewhere while an interrupt is open. */
export const APPROVAL_POLL_MS = 5000;

/** What the native data knows about a gated call (its card's arguments and deadline, a decision). */
export interface AgUiApprovalLookups {
  details: (runId: string, toolCallId: string) => { toolName: string; args: unknown; deadline: string | null; summary?: string | null } | null;
  decision: (runId: string, toolCallId: string) => ToolDecision | null;
}

/** The resume entry for a decision (`reason` is the comment, at most 500 characters server side). */
export function resumeEntry(interruptId: string, approved: boolean, comment = ""): AgUiResumeEntry {
  const reason = comment.trim();
  return { interruptId, status: "resolved", payload: reason ? { approved, reason } : { approved } };
}

/** Whether an interrupt's `expiresAt` (the approval deadline, D15) has passed. */
export function interruptExpired(interrupt: Pick<AgUiInterrupt, "expiresAt">, now = Date.now()): boolean {
  if (!interrupt.expiresAt) return false;
  const at = Date.parse(interrupt.expiresAt);
  return !Number.isNaN(at) && at <= now;
}

/**
 * The entries to send for a set of interrupts. An expired interrupt can only be answered
 * `cancelled` (the AG-UI spec; react-ag-ui refuses anything else), which the API records as a
 * denial unless a decision (here, the timeout) is already recorded, which then wins (D15).
 */
export function answerableEntries(interrupts: readonly AgUiInterrupt[], entries: readonly AgUiResumeEntry[], now = Date.now()): AgUiResumeEntry[] {
  return entries.map((entry, index) =>
    entry.status === "resolved" && interruptExpired(interrupts[index], now) ? { interruptId: entry.interruptId, status: "cancelled" } : entry,
  );
}

/** Sends retried before the chat offers to dismiss the interrupts (so the composer never stays locked). */
export const MAX_RESUME_TRIES = 3;

function targetOf(interrupt: AgUiInterrupt) {
  return toolInterruptTarget(interrupt);
}

export function AgUiApprovals({ lookups, onCount, onRefreshRun }: { lookups: AgUiApprovalLookups; onCount?: (count: number) => void; onRefreshRun?: (runId: string) => Promise<void> }) {
  const interrupts = useAgUiInterrupts();
  const submitResponses = useAgUiSubmitInterruptResponses();
  const ids = interrupts.map((interrupt) => interrupt.id).join("|");
  const [decided, setDecided] = useState<ReadonlyMap<string, AgUiResumeEntry>>(() => new Map());
  const [error, setError] = useState<string | null>(null);
  const sentRef = useRef<string | null>(null);
  // Failed sends of this set, and a tick that re-runs the send effect after a short wait.
  const [failures, setFailures] = useState(0);
  const [retryTick, setRetryTick] = useState(0);

  useEffect(() => {
    onCount?.(interrupts.length);
  }, [interrupts.length, onCount]);

  // Another set of interrupts: earlier decisions do not apply.
  useEffect(() => {
    setDecided(new Map());
    setError(null);
    setFailures(0);
    if (ids === "") sentRef.current = null;
  }, [ids]);

  // Decisions recorded elsewhere, from the native events.
  const recorded = useMemo(() => {
    const entries = new Map<string, AgUiResumeEntry>();
    for (const interrupt of interrupts) {
      const target = targetOf(interrupt);
      const decision = target ? lookups.decision(target.runId, target.toolCallId) : null;
      if (!decision) continue;
      // A timed-out call: its interrupt has expired, so only `cancelled` answers it (the recorded
      // timeout wins on the server).
      entries.set(interrupt.id, decision.kind === "timed_out" ? { interruptId: interrupt.id, status: "cancelled" } : resumeEntry(interrupt.id, decision.kind === "approved"));
    }
    return entries;
  }, [interrupts, lookups]);

  // Re-read the gated runs' events while interrupts are open.
  const runIds = useMemo(() => Array.from(new Set(interrupts.map((interrupt) => targetOf(interrupt)?.runId).filter((id): id is string => Boolean(id)))), [interrupts]);
  const runKey = runIds.join("|");
  useEffect(() => {
    if (!onRefreshRun || runKey === "") return;
    const timer = setInterval(() => {
      for (const runId of runKey.split("|")) void onRefreshRun(runId).catch(() => undefined);
    }, APPROVAL_POLL_MS);
    return () => clearInterval(timer);
  }, [onRefreshRun, runKey]);

  // Once every open interrupt has a decision, resume (once per set).
  useEffect(() => {
    if (interrupts.length === 0 || sentRef.current === ids || failures >= MAX_RESUME_TRIES) return;
    const entries = interrupts.map((interrupt) => decided.get(interrupt.id) ?? recorded.get(interrupt.id));
    if (entries.some((entry) => entry === undefined)) return;
    sentRef.current = ids;
    void submitResponses(answerableEntries(interrupts, entries as AgUiResumeEntry[])).catch(() => {
      // A run still settling, or a deadline crossed between the check and the send: re-read the
      // runs and try again shortly; after MAX_RESUME_TRIES the user can dismiss the interrupts.
      sentRef.current = null;
      setFailures((count) => count + 1);
      for (const runId of runKey.split("|")) if (runId) void onRefreshRun?.(runId).catch(() => undefined);
      setTimeout(() => setRetryTick((tick) => tick + 1), 1000);
    });
  }, [decided, failures, ids, interrupts, onRefreshRun, recorded, retryTick, runKey, submitResponses]);

  useEffect(() => {
    if (failures >= MAX_RESUME_TRIES) setError("The decision could not be sent. Dismiss the approval to keep chatting; the run's own state is kept.");
  }, [failures]);

  // The way out when sending keeps failing: answer every open interrupt `cancelled` (always
  // answerable, also after a deadline), so the composer is never left locked.
  const dismiss = useCallback(() => {
    setError(null);
    sentRef.current = ids;
    void submitResponses(interrupts.map((interrupt) => ({ interruptId: interrupt.id, status: "cancelled" as const }))).catch(() => {
      sentRef.current = null;
      setError("The approval could not be dismissed. Reload the page to reattach to the run.");
    });
  }, [ids, interrupts, submitResponses]);

  const decide = useCallback((interruptId: string, approved: boolean, comment: string) => {
    setError(null);
    setDecided((current) => new Map(current).set(interruptId, resumeEntry(interruptId, approved, comment)));
  }, []);

  if (interrupts.length === 0) return null;
  return (
    <div data-testid="agui-approvals" className="space-y-2">
      {interrupts.map((interrupt) => {
        const target = targetOf(interrupt);
        const pending = decided.has(interrupt.id) || recorded.has(interrupt.id);
        if (pending) {
          return (
            <p key={interrupt.id} data-testid="agui-approval-noted" data-interrupt-id={interrupt.id} className="my-2 rounded-md border border-border bg-surface px-3 py-2 text-caption text-muted-foreground">
              {recorded.has(interrupt.id) && !decided.has(interrupt.id) ? "Decided elsewhere; continuing the turn." : "Decision noted."}
              {interrupts.length > 1 ? " The turn continues once every call is answered." : null}
            </p>
          );
        }
        if (!target) {
          return (
            <p key={interrupt.id} data-testid="agui-interrupt" className="my-2 rounded-md border border-border bg-surface px-3 py-2 text-caption text-foreground">
              {interrupt.message ?? "The agent is waiting for input."}
            </p>
          );
        }
        const details = lookups.details(target.runId, target.toolCallId);
        return (
          <div key={interrupt.id} className="my-2" data-testid="tool-approval-waiting" data-tool-call-id={target.toolCallId} data-interrupt-id={interrupt.id}>
            <ApprovalCard
              approval={{
                kind: "tool",
                runId: target.runId,
                toolCallId: target.toolCallId,
                toolName: details?.toolName ?? target.toolName ?? "tool",
                args: details?.args ?? {},
                deadline: details?.deadline ?? interrupt.expiresAt ?? null,
                summary: details?.summary ?? null,
              }}
              submit={async (decision, comment) => decide(interrupt.id, decision === "approve", comment)}
              onRefresh={() => onRefreshRun?.(target.runId)}
            />
          </div>
        );
      })}
      {error ? (
        <div role="alert" data-testid="agui-approval-error" className="flex min-w-0 flex-wrap items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-label text-destructive-text">
          <span className="min-w-0 break-words">{error}</span>
          {failures >= MAX_RESUME_TRIES ? (
            <Button size="sm" variant="secondary" data-testid="agui-approval-dismiss" onClick={dismiss}>
              Dismiss
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
