"use client";

import React, { memo, useCallback, useEffect, useMemo, useRef } from "react";
import {
  formatDuration,
  spanGeometry,
  spanLabel,
  type NameMaps,
  type SpanKind,
  type SpanRow,
  type SpanStatus,
  type TraceWindow,
} from "@/lib/traces";
import { KIND_BADGE, STATUS_BAR, STATUS_LABELS } from "./spanStyles";

/** "timeline": each span's bar on the trace's time axis (the waterfall). "tree": the span tree alone. */
export type SpanTreeMode = "timeline" | "tree";

interface TraceWaterfallProps {
  rows: SpanRow[];
  window: TraceWindow | null;
  selectedId: string | null;
  onSelect: (spanId: string) => void;
  collapsed: ReadonlySet<string>;
  onToggle: (spanId: string) => void;
  names?: NameMaps;
  /** Fixed "now" for open spans (tests); defaults to the render time. */
  now?: number;
  /** Timeline (default) or Tree: the same rows, without the time axis and bars. */
  mode?: SpanTreeMode;
}

interface RowItemProps {
  spanId: string;
  kind: SpanKind;
  status: SpanStatus;
  label: string;
  depth: number;
  expandable: boolean;
  isCollapsed: boolean;
  selected: boolean;
  focusable: boolean;
  offsetPct: number;
  widthPct: number;
  zeroLength: boolean;
  duration: string;
  mode: SpanTreeMode;
  onSelect: (spanId: string) => void;
  onToggle: (spanId: string) => void;
  onKey: (event: React.KeyboardEvent, spanId: string) => void;
  registerRef: (spanId: string, el: HTMLDivElement | null) => void;
}

/** One waterfall row. Every prop is a primitive or a stable callback, so a poll or an arrow key
 * re-renders only the rows whose selection, focus, collapse state or geometry changed. */
const RowItem = memo(function RowItem({
  spanId, kind, status, label, depth, expandable, isCollapsed, selected, focusable,
  offsetPct, widthPct, zeroLength, duration, mode, onSelect, onToggle, onKey, registerRef,
}: RowItemProps) {
  const timeline = mode === "timeline";
  return (
    <div
      ref={(el) => registerRef(spanId, el)}
      role="treeitem"
      aria-level={depth + 1}
      aria-selected={selected}
      aria-expanded={expandable ? !isCollapsed : undefined}
      aria-label={`${label}, ${STATUS_LABELS[status]}, ${duration}`}
      tabIndex={focusable ? 0 : -1}
      data-testid="trace-span-row"
      data-span-id={spanId}
      data-kind={kind}
      data-status={status}
      data-depth={depth}
      onClick={() => onSelect(spanId)}
      onKeyDown={(e) => onKey(e, spanId)}
      className={`flex cursor-pointer items-center px-3 py-1.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring ${
        selected ? "bg-accent text-accent-foreground" : "hover:bg-muted/60"
      }`}
    >
      <div className={`flex ${timeline ? "w-2/5 shrink-0" : "min-w-0 flex-1"} items-center gap-1 overflow-hidden pr-2`} style={{ paddingLeft: `${depth * 16}px` }}>
        {expandable ? (
          <button
            type="button"
            tabIndex={-1}
            aria-label={isCollapsed ? `Expand ${label}` : `Collapse ${label}`}
            onClick={(e) => {
              e.stopPropagation();
              onToggle(spanId);
            }}
            className="w-4 shrink-0 text-xs text-muted-foreground hover:text-card-foreground"
          >
            {isCollapsed ? "▸" : "▾"}
          </button>
        ) : (
          <span className="w-4 shrink-0" aria-hidden="true" />
        )}
        <span className="shrink-0 rounded bg-muted px-1 font-mono text-[10px] text-muted-foreground">
          {KIND_BADGE[kind]}
        </span>
        <span className="truncate" title={label}>{label}</span>
        {timeline ? null : (
          <span data-testid="trace-span-dot" className={`ml-1 inline-block size-2 shrink-0 rounded-full ${STATUS_BAR[status]}`} aria-hidden="true" />
        )}
      </div>
      {timeline ? (
      <div className="relative h-5 flex-1" aria-hidden="true">
        <div className="absolute inset-y-2 left-0 right-0 rounded bg-muted/40" />
        {zeroLength ? (
          <div
            data-testid="trace-span-marker"
            className={`absolute inset-y-0.5 w-1 rounded ${STATUS_BAR[status]}`}
            style={{ left: `calc(${offsetPct}% - 2px)` }}
          />
        ) : (
          <div
            data-testid="trace-span-bar"
            className={`absolute inset-y-0.5 rounded ${STATUS_BAR[status]}`}
            style={{ left: `${offsetPct}%`, width: `max(${widthPct}%, 2px)` }}
          />
        )}
      </div>
      ) : null}
      <div className="w-20 shrink-0 pl-2 text-right font-mono text-xs text-muted-foreground">{duration}</div>
    </div>
  );
});

/** The span tree as a waterfall: one row per visible span, indented by depth, with a bar placed
 * on the trace's time axis and coloured by status. A WAI-ARIA tree: Up/Down move, Right expands or
 * enters the first child, Left collapses or goes to the parent, Home/End jump. */
export function TraceWaterfall({
  rows, window, selectedId, onSelect, collapsed, onToggle, names, now, mode = "timeline",
}: TraceWaterfallProps) {
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const focusPending = useRef(false);
  const at = now ?? Date.now();

  const indexById = useMemo(() => new Map(rows.map((r, i) => [r.span.id, i])), [rows]);

  // The key handler reads the latest rows and state through a ref, so it stays one stable function.
  const latest = useRef({ rows, indexById, collapsed, onSelect, onToggle });
  latest.current = { rows, indexById, collapsed, onSelect, onToggle };

  useEffect(() => {
    if (!focusPending.current || !selectedId) return;
    focusPending.current = false;
    rowRefs.current.get(selectedId)?.focus();
  }, [selectedId]);

  const registerRef = useCallback((spanId: string, el: HTMLDivElement | null) => {
    if (el) rowRefs.current.set(spanId, el);
    else rowRefs.current.delete(spanId);
  }, []);

  const onKey = useCallback((event: React.KeyboardEvent, spanId: string) => {
    const { rows, indexById, collapsed, onSelect, onToggle } = latest.current;
    const index = indexById.get(spanId);
    if (index === undefined) return;
    const row = rows[index];
    const move = (id: string) => {
      focusPending.current = true;
      onSelect(id);
    };
    const expandable = row.children > 0;
    const isCollapsed = collapsed.has(spanId);
    const parentId = row.span.parent_id;
    switch (event.key) {
      case "ArrowDown":
        if (index + 1 < rows.length) move(rows[index + 1].span.id);
        break;
      case "ArrowUp":
        if (index > 0) move(rows[index - 1].span.id);
        break;
      case "Home":
        if (rows.length) move(rows[0].span.id);
        break;
      case "End":
        if (rows.length) move(rows[rows.length - 1].span.id);
        break;
      case "ArrowRight":
        if (expandable && isCollapsed) onToggle(spanId);
        else if (expandable && index + 1 < rows.length) move(rows[index + 1].span.id);
        break;
      case "ArrowLeft":
        if (expandable && !isCollapsed) onToggle(spanId);
        else if (parentId && indexById.has(parentId)) move(parentId);
        break;
      case "Enter":
      case " ":
        onSelect(spanId);
        break;
      default:
        return;
    }
    event.preventDefault();
  }, []);

  const focusId = selectedId && indexById.has(selectedId) ? selectedId : rows[0]?.span.id;

  return (
    <div className="rounded-md border border-border bg-card" data-span-mode={mode}>
      <div className="flex border-b border-border bg-muted/50 px-3 py-2 text-xs font-medium text-muted-foreground">
        {mode === "timeline" ? (
          <>
            <div className="w-2/5 shrink-0">Span</div>
            <div className="flex flex-1 justify-between">
              <span>0 ms</span>
              <span>{formatDuration(window?.durationMs ?? null)}</span>
            </div>
          </>
        ) : (
          <>
            <div className="min-w-0 flex-1">Span</div>
            <div className="w-20 shrink-0 text-right">{formatDuration(window?.durationMs ?? null)}</div>
          </>
        )}
      </div>
      <div role="tree" aria-label="Trace spans" className="max-h-[32rem] overflow-auto">
        {rows.map((row) => {
          const { span, depth } = row;
          const geom = spanGeometry(span, window, at);
          return (
            <RowItem
              key={span.id}
              spanId={span.id}
              kind={span.kind}
              status={span.status}
              label={spanLabel(span, names)}
              depth={depth}
              expandable={row.children > 0}
              isCollapsed={collapsed.has(span.id)}
              selected={span.id === selectedId}
              focusable={span.id === focusId}
              offsetPct={geom.offsetPct}
              widthPct={geom.widthPct}
              zeroLength={geom.zeroLength}
              duration={geom.open ? "running" : formatDuration(geom.durationMs)}
              mode={mode}
              onSelect={onSelect}
              onToggle={onToggle}
              onKey={onKey}
              registerRef={registerRef}
            />
          );
        })}
      </div>
      <div className="flex flex-wrap gap-3 border-t border-border px-3 py-2 text-xs text-muted-foreground" aria-label="Status legend">
        {(Object.keys(STATUS_LABELS) as SpanStatus[]).map((status) => (
          <span key={status} className="inline-flex items-center gap-1">
            <span className={`inline-block h-2 w-3 rounded ${STATUS_BAR[status]}`} aria-hidden="true" />
            {STATUS_LABELS[status]}
          </span>
        ))}
      </div>
    </div>
  );
}
