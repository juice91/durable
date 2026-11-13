import Link from "next/link";
import React, { useMemo, useState } from "react";
import { LogLevelBadge } from "./LogLevelBadge";
import { formatLogTime, logRowKeys, type LogItem } from "./logsQuery";
import { routes } from "@/lib/routes";

interface LogsTableProps {
  items: LogItem[];
  caption: string;
  /** Show the "Trace" column linking each record to its run's trace (off inside the trace view). */
  showTraceLink?: boolean;
  busy?: boolean;
}

/** Messages longer than this many characters are shown cut, with a control to expand them. */
export const MESSAGE_PREVIEW_CHARS = 2000;

function LogMessage({ message }: { message: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = message.length > MESSAGE_PREVIEW_CHARS;
  const shown = long && !expanded ? `${message.slice(0, MESSAGE_PREVIEW_CHARS)}...` : message;
  return (
    <>
      <pre className="whitespace-pre-wrap font-mono text-xs [overflow-wrap:anywhere]">{shown}</pre>
      {long ? (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {expanded ? "Show less" : `Show full message (${message.length.toLocaleString("en-US")} characters)`}
        </button>
      ) : null}
    </>
  );
}

function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}...` : id;
}

/** Log records as a table: time, level, service, message and, optionally, the run's trace.
 * Memoised: a parent re-render with the same items does not re-render every row. */
export const LogsTable = React.memo(function LogsTable({ items, caption, showTraceLink = true, busy = false }: LogsTableProps) {
  const rowKeys = useMemo(() => logRowKeys(items), [items]);
  return (
    <div className="overflow-x-auto rounded-md border border-border bg-card text-card-foreground">
      <table className="w-full text-sm" aria-busy={busy || undefined} data-testid="logs-table">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-muted/50">
          <tr>
            <th scope="col" className="px-3 py-2 text-left font-medium text-muted-foreground">Time</th>
            <th scope="col" className="px-3 py-2 text-left font-medium text-muted-foreground">Level</th>
            <th scope="col" className="px-3 py-2 text-left font-medium text-muted-foreground">Service</th>
            <th scope="col" className="px-3 py-2 text-left font-medium text-muted-foreground">Message</th>
            {showTraceLink ? <th scope="col" className="px-3 py-2 text-left font-medium text-muted-foreground">Trace</th> : null}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {items.map((item, index) => {
            const { time, date } = formatLogTime(item.ts);
            return (
              <tr key={rowKeys[index]} data-testid="log-row" className="align-top hover:bg-muted/30">
                <td className="whitespace-nowrap px-3 py-2 font-mono text-xs">
                  <time dateTime={item.ts} title={`${date} ${time}`}>{time}</time>
                  <div className="text-[11px] text-muted-foreground">{date}</div>
                </td>
                <td className="px-3 py-2"><LogLevelBadge level={item.level} /></td>
                <td className="whitespace-nowrap px-3 py-2 text-xs text-muted-foreground">{item.service ?? "-"}</td>
                <td className="min-w-0 px-3 py-2">
                  <LogMessage message={item.message} />
                </td>
                {showTraceLink ? (
                  <td className="whitespace-nowrap px-3 py-2 text-xs">
                    {item.run_id ? (
                      <Link
                        href={routes.trace(item.run_id)}
                        aria-label={`Open trace of run ${item.run_id}`}
                        className="font-mono text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {shortId(item.run_id)}
                      </Link>
                    ) : (
                      <span className="text-muted-foreground" title="Not logged inside a run">-</span>
                    )}
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
});
