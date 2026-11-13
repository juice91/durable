import React from "react";

const LEVEL_STYLES: Record<string, string> = {
  debug: "bg-muted text-muted-foreground",
  info: "bg-accent text-accent-foreground",
  warn: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300",
  error: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
  fatal: "bg-destructive text-destructive-foreground",
};

/** A log record's level as text (never colour alone); unknown or missing levels are muted. */
export function LogLevelBadge({ level }: { level: string | null }) {
  const text = level ?? "unknown";
  const style = LEVEL_STYLES[text] ?? "bg-muted text-muted-foreground";
  return (
    <span
      data-testid="log-level"
      className={`inline-flex items-center rounded px-1.5 py-0.5 font-mono text-[11px] font-semibold uppercase ${style}`}
    >
      {text}
    </span>
  );
}
