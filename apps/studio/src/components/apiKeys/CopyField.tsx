"use client";

import React, { useId, useState } from "react";
import { Check, Copy } from "lucide-react";
import { copyText } from "@/lib/clipboard";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button } from "@/components/ui/Button";

export interface CopyFieldProps {
  label: string;
  value: string;
  /** A code block (snippets) instead of one line (URLs, the new key). */
  multiline?: boolean;
  /** What the status line says once copied ("Key copied"); never the value itself. */
  copiedMessage?: string;
  testId?: string;
  hint?: React.ReactNode;
}

/**
 * A labelled, read-only value with a Copy button. The copy result goes to a polite status line, not
 * a toast, so the value is never repeated anywhere else on the page.
 */
export function CopyField({ label, value, multiline = false, copiedMessage = "Copied", testId, hint }: CopyFieldProps) {
  const id = useId();
  const [status, setStatus] = useState<string | null>(null);
  const copy = async () => setStatus((await copyText(value)) ? copiedMessage : "Copying is not available here; select the text and copy it.");
  const Icon = status === copiedMessage ? Check : Copy;
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid={testId}>
      <div className="flex min-w-0 items-center justify-between gap-2">
        <span id={`${id}-label`} className="text-label font-medium text-foreground">
          {label}
        </span>
        <Button variant="secondary" size="sm" onClick={() => void copy()} aria-describedby={`${id}-label`} data-copy-button="">
          <Icon strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          Copy
        </Button>
      </div>
      {multiline ? (
        <pre
          aria-labelledby={`${id}-label`}
          tabIndex={0}
          className="max-h-80 min-w-0 overflow-auto rounded-md border border-border bg-muted/40 p-3 font-mono text-caption text-foreground"
          data-copy-value=""
        >
          <code>{value}</code>
        </pre>
      ) : (
        <input
          aria-labelledby={`${id}-label`}
          readOnly
          value={value}
          onFocus={(event) => event.currentTarget.select()}
          className="w-full min-w-0 rounded-md border border-input bg-surface px-3 py-1.5 font-mono text-caption text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
          data-copy-value=""
        />
      )}
      {hint ? <p className="text-caption text-muted-foreground">{hint}</p> : null}
      <p role="status" className="min-h-4 text-caption text-muted-foreground">
        {status}
      </p>
    </div>
  );
}
