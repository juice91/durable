"use client";

import React from "react";
import { formatTokens, formatUsd } from "@/lib/format";
import {  } from "@/lib/metrics";
import { tracePath } from "@/lib/traces";
import type { RunStatus } from "@/lib/runs";
import { versionLabel, type BlockCode, type TurnUsage } from "./runOptions";

/** What the thread shows under one turn's last message. */
export interface TurnView {
  runId: string;
  status: RunStatus;
  modelId: string | null;
  modelOverridden: boolean;
  draft: boolean;
  version: number | null;
  usage: TurnUsage;
  /** The target has output processors and the turn is still running: no live text is streamed. */
  checking: boolean;
  block: { code: BlockCode; message: string | null } | null;
  /** Whether live deltas may be shown for this turn (false: withheld, see Turn.liveText). */
  liveText: boolean;
  /** The block happened while the page watched (not on reload or a thread switch): announce it. */
  announce: boolean;
}

const BLOCK_TITLES: Record<BlockCode, string> = {
  input_blocked: "Message blocked",
  output_blocked: "Answer blocked",
};

const BLOCK_FALLBACK: Record<BlockCode, string> = {
  input_blocked: "The message was blocked by this agent's input processors.",
  output_blocked: "The answer was blocked by this agent's output processors.",
};

function sentence(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return trimmed;
  const capital = trimmed[0].toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}

function costText(usage: TurnUsage): string {
  if (!usage.recorded) return "";
  if (!usage.measured) return "cost not reported";
  return usage.costUsd === null ? "no price" : formatUsd(usage.costUsd);
}

function tokensText(usage: TurnUsage, running: boolean): string {
  if (!usage.recorded) return running ? "tokens pending" : "usage not recorded";
  // The provider reported no usage for some request: its zeros are not real counts.
  if (!usage.measured) return "tokens not reported";
  return `${formatTokens(usage.inputTokens)} in · ${formatTokens(usage.outputTokens)} out tokens`;
}

export function TurnFooter({ turn }: { turn: TurnView }) {
  const running = !["completed", "failed", "needs_attention"].includes(turn.status);
  const version = versionLabel(turn.draft, turn.version);
  return (
    <div className="mt-2 w-full space-y-2" data-testid="turn-footer" data-run-id={turn.runId} data-live-text={String(turn.liveText)}>
      {turn.checking && (
        <div
          role="status"
          data-testid="turn-output-checking"
          className="flex items-center gap-2 rounded-md border border-border bg-muted px-3 py-2 text-sm text-muted-foreground"
        >
          <span aria-hidden="true" className="inline-block h-2 w-2 animate-pulse rounded-full bg-primary" />
          The output is being checked before display.
        </div>
      )}
      {turn.block && (
        <div
          role={turn.announce ? "alert" : undefined}
          data-testid="turn-blocked"
          data-code={turn.block.code}
          className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive-text"
        >
          <p className="font-medium">{BLOCK_TITLES[turn.block.code]}</p>
          <p>{turn.block.message ? sentence(turn.block.message) : BLOCK_FALLBACK[turn.block.code]}</p>
        </div>
      )}
      <div
        data-testid="turn-badge"
        className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
      >
        <span data-testid="turn-badge-model" className="font-mono">
          {turn.modelId ?? "model pending"}
        </span>
        {turn.modelOverridden && (
          <span className="rounded-full bg-accent px-2 py-0.5 text-accent-foreground">override</span>
        )}
        {version && (
          <span data-testid="turn-badge-version" className="rounded-full border border-border px-2 py-0.5">
            {version}
          </span>
        )}
        <span data-testid="turn-badge-tokens">{tokensText(turn.usage, running)}</span>
        {turn.usage.recorded && <span data-testid="turn-badge-cost">{costText(turn.usage)}</span>}
        <a href={tracePath(turn.runId)} className="text-primary hover:underline">
          Open trace
        </a>
      </div>
    </div>
  );
}
