"use client";

/**
 * Publish-time advice from a publish or restore response (AC.7 `warnings`: an unpriced model under a
 * cost cap, a redacting processor with fixed output strings). It never blocks the publish. API text
 * is rendered as plain text. Tokens only.
 */
import React from "react";
import type { PublishWarning } from "./agentCapabilities";

export function PublishWarnings({ warnings, className = "" }: { warnings: readonly PublishWarning[]; className?: string }) {
  if (warnings.length === 0) return null;
  return (
    <div data-testid="publish-warnings" className={`rounded-md border border-warning/40 bg-warning/10 p-3 text-label text-warning-text ${className}`}>
      <p className="font-medium">Published with warnings</p>
      <ul className="mt-1 list-inside list-disc space-y-0.5 break-words">
        {warnings.map((warning) => (
          <li key={`${warning.code}:${warning.path}`} data-warning-code={warning.code}>
            {warning.message}
          </li>
        ))}
      </ul>
    </div>
  );
}
