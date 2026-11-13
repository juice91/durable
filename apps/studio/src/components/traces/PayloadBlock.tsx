"use client";

import React, { useState } from "react";

/** Characters shown before a payload is collapsed behind "Show all". */
export const PAYLOAD_PREVIEW_CHARS = 600;

export function payloadText(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

interface PayloadBlockProps {
  label: string;
  value: unknown;
  /** Original size in bytes when the API cut this field to a preview. */
  truncatedBytes?: number;
}

/** A span's input or output: pretty JSON (or text), collapsed when long, with a notice when the
 * server sent only a preview. */
export function PayloadBlock({ label, value, truncatedBytes }: PayloadBlockProps) {
  const [expanded, setExpanded] = useState(false);
  const id = `payload-${label.toLowerCase().replace(/\W+/g, "-")}`;
  if (value === null || value === undefined) {
    return (
      <div data-testid={id}>
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{label}</h4>
        <p className="mt-1 text-sm text-muted-foreground">None</p>
      </div>
    );
  }
  const text = payloadText(value);
  const long = text.length > PAYLOAD_PREVIEW_CHARS;
  const shown = long && !expanded ? `${text.slice(0, PAYLOAD_PREVIEW_CHARS)}…` : text;
  return (
    <div data-testid={id}>
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{label}</h4>
        {long ? (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            aria-controls={`${id}-body`}
            className="text-xs text-primary underline-offset-4 hover:underline"
          >
            {expanded ? "Collapse" : `Show all (${text.length.toLocaleString("en-US")} characters)`}
          </button>
        ) : null}
      </div>
      {truncatedBytes !== undefined ? (
        <p role="note" className="mt-1 text-xs text-muted-foreground" data-testid={`${id}-truncated`}>
          Truncated by the server: showing a preview of {formatBytes(truncatedBytes)}.
        </p>
      ) : null}
      <pre
        id={`${id}-body`}
        className="mt-1 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-2 font-mono text-xs text-card-foreground"
      >
        {shown}
      </pre>
    </div>
  );
}
