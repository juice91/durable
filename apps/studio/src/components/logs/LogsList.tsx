"use client";

import React, { useMemo } from "react";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { TraceLink } from "@/components/traces/TraceLink";
import { LogLevelBadge } from "./LogLevelBadge";
import { LogMessage, shortId } from "./LogsTable";
import { formatLogTime, logRowKeys, type LogItem } from "./logsQuery";

interface LogRow {
  key: string;
  item: LogItem;
}

interface LogsListProps {
  items: LogItem[];
  /** The table's accessible name. */
  caption: string;
  busy?: boolean;
  /** The row whose log Sheet is open. */
  selectedKey?: string | null;
  /** A row opens its record (the log Sheet). */
  onOpen: (item: LogItem, key: string) => void;
}

const columns: DataListColumn<LogRow>[] = [
  {
    id: "time",
    header: "Time",
    className: "whitespace-nowrap align-top",
    cell: ({ item }) => {
      const { time, date } = formatLogTime(item.ts);
      return (
        <span className="font-mono text-caption">
          <time dateTime={item.ts} title={`${date} ${time}`}>{time}</time>
          <span className="block text-badge text-muted-foreground">{date}</span>
        </span>
      );
    },
  },
  { id: "level", header: "Level", className: "align-top", cell: ({ item }) => <LogLevelBadge level={item.level} /> },
  {
    id: "service",
    header: "Service",
    className: "whitespace-nowrap align-top text-caption text-muted-foreground",
    cell: ({ item }) => item.service ?? "-",
  },
  { id: "message", header: "Message", className: "min-w-0 align-top", cell: ({ item }) => <LogMessage message={item.message} /> },
  {
    id: "trace",
    header: "Trace",
    className: "whitespace-nowrap align-top text-caption",
    cell: ({ item }) =>
      item.run_id ? (
        <TraceLink
          runId={item.run_id}
          aria-label={`Open trace of run ${item.run_id}`}
          className="font-mono text-info-text hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {shortId(item.run_id)}
        </TraceLink>
      ) : (
        <span className="text-muted-foreground" title="Not logged inside a run">-</span>
      ),
  },
];

/** The Logs page's records as a DataList (SL.12): a row opens the record's Sheet; the Trace column
 * opens the run's trace Sheet. Newest first, as the API returns them. */
export const LogsList = React.memo(function LogsList({ items, caption, busy = false, selectedKey = null, onOpen }: LogsListProps) {
  const rows = useMemo(() => {
    const keys = logRowKeys(items);
    return items.map((item, index) => ({ key: keys[index], item }));
  }, [items]);
  return (
    <div aria-busy={busy || undefined} data-testid="logs-table">
      <DataList<LogRow>
        label={caption}
        rows={rows}
        columns={columns}
        getRowId={(row) => row.key}
        onRowOpen={(row) => onOpen(row.item, row.key)}
        getRowLabel={(row) => `Open the log record from ${formatLogTime(row.item.ts).time}`}
        getRowTestId={() => "log-row"}
        selectedRowId={selectedKey}
        density="compact"
      />
    </div>
  );
});
