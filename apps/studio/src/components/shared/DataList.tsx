"use client";

import React, { useCallback, useEffect, useId, useImperativeHandle, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { ArrowDown, ArrowUp, ChevronsUpDown, Search, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button } from "@/components/ui/Button";
import { Kbd } from "@/components/ui/Kbd";
import { Skeleton } from "@/components/ui/Skeleton";
import { EmptyState } from "./EmptyState";

/** Debounce for the search box, in ms. */
export const DATALIST_SEARCH_DEBOUNCE_MS = 300;

export type SortDirection = "asc" | "desc";
export interface SortState {
  columnId: string;
  direction: SortDirection;
}

export interface DataListColumn<Row> {
  id: string;
  header: React.ReactNode;
  /** Cell content. The first column's content becomes the row's link (or open button). */
  cell: (row: Row) => React.ReactNode;
  sortable?: boolean;
  /** Sort key; required for sortable columns whose `cell` is not a string or number. */
  sortValue?: (row: Row) => string | number | null | undefined;
  align?: "start" | "end";
  /** Classes for the cells of this column (widths: `w-32`, `hidden md:table-cell`…). */
  className?: string;
  headerClassName?: string;
}

export interface DataListEmpty {
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
}

export interface DataListProps<Row> {
  /** Accessible name of the table ("Agents"). */
  label: string;
  rows: Row[];
  columns: DataListColumn<Row>[];
  getRowId: (row: Row) => string;
  /** Rows are links: the first cell renders a Next link, and a click anywhere on the row follows it. */
  getRowHref?: (row: Row) => string;
  /** Rows open something in place (a Sheet): the first cell renders a button. Ignored when `getRowHref` is set. */
  onRowOpen?: (row: Row) => void;
  /** Accessible name of a row's link or button; defaults to the first cell's text. */
  getRowLabel?: (row: Row) => string;
  getRowTestId?: (row: Row) => string | undefined;
  /** The row whose Sheet is open, highlighted. */
  selectedRowId?: string | null;

  /** Show the search box with this placeholder. */
  searchPlaceholder?: string;
  /** Accessible name of the search box; defaults to the placeholder. */
  searchLabel?: string;
  /** Client-side filter applied with the debounced query. Without it, rows are not filtered here. */
  filterRow?: (row: Row, query: string) => boolean;
  /** Called with the debounced query (for server-side search). */
  onSearchChange?: (query: string) => void;
  initialQuery?: string;
  /** Pressing `/` outside a field focuses the search box. Default true. */
  searchShortcut?: boolean;
  /** Extra controls on the search row (filters, view toggle, create button). */
  toolbar?: React.ReactNode;

  defaultSort?: SortState | null;
  /** Controlled sort. */
  sort?: SortState | null;
  onSortChange?: (sort: SortState | null) => void;
  /** Rows arrive sorted (server side): headers still toggle, but rows are not re-sorted here. */
  manualSort?: boolean;

  /** The state ladder: loading → error → empty → no match → rows. */
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  empty?: DataListEmpty;
  /** Text when a search matches nothing. */
  noMatchText?: string;

  density?: "default" | "compact";
  className?: string;
  testId?: string;
}

const INTERACTIVE = "a, button, input, select, textarea, label, summary, [role='button'], [role='link'], [role='menuitem'], [role='checkbox'], [role='switch'], [contenteditable='true']";

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || target.matches("input, textarea, select, [contenteditable='true']");
}

function primitiveSortValue(node: React.ReactNode): string | number | null {
  return typeof node === "string" || typeof node === "number" ? node : null;
}

/** Compares sort keys: numbers numerically, strings with locale and numeric collation, empties last. */
export function compareSortValues(a: string | number | null | undefined, b: string | number | null | undefined): number {
  const aEmpty = a === null || a === undefined || a === "";
  const bEmpty = b === null || b === undefined || b === "";
  if (aEmpty || bEmpty) return aEmpty === bEmpty ? 0 : aEmpty ? 1 : -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/** Next sort after clicking a header: ascending, then descending, then unsorted. */
export function nextSort(current: SortState | null, columnId: string): SortState | null {
  if (!current || current.columnId !== columnId) return { columnId, direction: "asc" };
  if (current.direction === "asc") return { columnId, direction: "desc" };
  return null;
}

/** Returns `value` once it has stopped changing for `delay` ms. */
export function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export interface DataListSearchHandle {
  focus: () => void;
  /** Empty the box and report "" at once (no debounce). */
  clear: () => void;
}

interface DataListSearchProps {
  placeholder: string;
  label: string;
  initialQuery: string;
  shortcut: boolean;
  /** The debounced query; called only when it changes. */
  onQueryChange: (query: string) => void;
  /** ArrowDown in the box: move to the first row. */
  onArrowDown: () => void;
}

/**
 * The search box. It owns the raw text, so typing re-renders only this component; the list hears
 * about the query once it has been stable for 300 ms. `/` focuses it, unless typing in a field, a
 * modifier is held or an overlay is open.
 */
const DataListSearch = React.memo(
  React.forwardRef<DataListSearchHandle, DataListSearchProps>(function DataListSearch({ placeholder, label, initialQuery, shortcut, onQueryChange, onArrowDown }, ref) {
    const id = useId();
    const inputRef = useRef<HTMLInputElement>(null);
    const [query, setQuery] = useState(initialQuery);
    const debounced = useDebouncedValue(query, DATALIST_SEARCH_DEBOUNCE_MS);
    const reported = useRef(initialQuery);
    const onQueryChangeRef = useRef(onQueryChange);
    onQueryChangeRef.current = onQueryChange;

    useEffect(() => {
      if (debounced === reported.current) return;
      reported.current = debounced;
      onQueryChangeRef.current(debounced);
    }, [debounced]);

    const clear = useCallback(() => {
      setQuery("");
      if (reported.current !== "") {
        reported.current = "";
        onQueryChangeRef.current("");
      }
    }, []);

    useImperativeHandle(ref, () => ({ focus: () => inputRef.current?.focus(), clear }), [clear]);

    useEffect(() => {
      if (!shortcut) return;
      const onKeyDown = (event: KeyboardEvent) => {
        if (event.key !== "/" || event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
        if (isEditableTarget(event.target)) return;
        if (document.querySelector("[role='dialog'], [role='alertdialog'], [role='menu']")) return;
        event.preventDefault();
        inputRef.current?.focus();
      };
      document.addEventListener("keydown", onKeyDown);
      return () => document.removeEventListener("keydown", onKeyDown);
    }, [shortcut]);

    return (
      <div className="relative min-w-0 flex-1 basis-56">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-icon -translate-y-1/2 text-muted-foreground" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        <label htmlFor={id} className="sr-only">{label}</label>
        <input
          ref={inputRef}
          id={id}
          type="search"
          data-list-search=""
          value={query}
          placeholder={placeholder}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              onArrowDown();
            } else if (event.key === "Escape" && query) {
              event.preventDefault();
              clear();
            }
          }}
          aria-keyshortcuts={shortcut ? "/" : undefined}
          className="h-8 w-full min-w-0 rounded-md border border-input bg-surface pl-8 pr-8 text-label text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-search-cancel-button]:hidden"
        />
        {query ? (
          <button
            type="button"
            aria-label="Clear search"
            onClick={() => {
              clear();
              inputRef.current?.focus();
            }}
            className="absolute right-1.5 top-1/2 flex size-6 -translate-y-1/2 items-center justify-center rounded-sm text-muted-foreground outline-none hover:bg-fill-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            <X className="size-icon-dense" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          </button>
        ) : shortcut ? (
          <Kbd aria-hidden="true" className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2">/</Kbd>
        ) : null}
      </div>
    );
  }),
);

/**
 * The shared list: a real table with sortable headers (`aria-sort`), link or open-button rows that
 * are clickable as a whole, ArrowUp / ArrowDown / Home / End between rows and Enter to open, a
 * debounced search box focused by `/`, and the loading, error, empty and no-match states.
 */
export function DataList<Row>({
  label,
  rows,
  columns,
  getRowId,
  getRowHref,
  onRowOpen,
  getRowLabel,
  getRowTestId,
  selectedRowId = null,
  searchPlaceholder,
  searchLabel,
  filterRow,
  onSearchChange,
  initialQuery = "",
  searchShortcut = true,
  toolbar,
  defaultSort = null,
  sort: sortProp,
  onSortChange,
  manualSort = false,
  loading = false,
  error = null,
  onRetry,
  empty,
  noMatchText = "No matching results.",
  density = "default",
  className,
  testId,
}: DataListProps<Row>) {
  const searchRef = useRef<DataListSearchHandle>(null);
  const bodyRef = useRef<HTMLTableSectionElement>(null);
  // The debounced query only: raw keystrokes stay inside DataListSearch.
  const [debouncedQuery, setDebouncedQuery] = useState(initialQuery);
  const trimmedQuery = debouncedQuery.trim();
  const hasSearch = searchPlaceholder !== undefined;

  const [uncontrolledSort, setUncontrolledSort] = useState<SortState | null>(defaultSort);
  const sort = sortProp !== undefined ? sortProp : uncontrolledSort;

  const onSearchChangeRef = useRef(onSearchChange);
  onSearchChangeRef.current = onSearchChange;
  const handleQueryChange = useCallback((next: string) => {
    setDebouncedQuery(next);
    onSearchChangeRef.current?.(next);
  }, []);

  const visibleRows = useMemo(() => {
    let result = rows;
    if (filterRow && trimmedQuery) result = result.filter((row) => filterRow(row, trimmedQuery));
    if (sort && !manualSort) {
      const column = columns.find((candidate) => candidate.id === sort.columnId);
      if (column) {
        const key = column.sortValue ?? ((row: Row) => primitiveSortValue(column.cell(row)));
        const factor = sort.direction === "asc" ? 1 : -1;
        result = result
          .map((row, index) => ({ row, index, value: key(row) }))
          .sort((a, b) => {
            const aEmpty = a.value === null || a.value === undefined || a.value === "";
            const bEmpty = b.value === null || b.value === undefined || b.value === "";
            // Empty values stay last in both directions; ties keep their original order.
            if (aEmpty || bEmpty) return aEmpty === bEmpty ? a.index - b.index : aEmpty ? 1 : -1;
            return compareSortValues(a.value, b.value) * factor || a.index - b.index;
          })
          .map((entry) => entry.row);
      }
    }
    return result;
  }, [columns, filterRow, manualSort, rows, sort, trimmedQuery]);

  const toggleSort = (columnId: string) => {
    const next = nextSort(sort, columnId);
    if (sortProp === undefined) setUncontrolledSort(next);
    onSortChange?.(next);
  };

  const primaries = useCallback(() => Array.from(bodyRef.current?.querySelectorAll<HTMLElement>("[data-datalist-primary]") ?? []), []);

  const focusRow = useCallback(
    (index: number) => {
      const list = primaries();
      if (list.length === 0) return;
      list[Math.max(0, Math.min(index, list.length - 1))].focus();
    },
    [primaries],
  );
  const focusFirstRow = useCallback(() => focusRow(0), [focusRow]);

  const onBodyKeyDown = (event: React.KeyboardEvent<HTMLTableSectionElement>) => {
    // React bubbles events from portaled content (a popover or dialog opened from a cell) through
    // the component tree: only keys from the table's own DOM are row navigation.
    if (event.defaultPrevented || !event.currentTarget.contains(event.target as Node)) return;
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (isEditableTarget(event.target)) return;
    const list = primaries();
    const row = (event.target as HTMLElement).closest("tr");
    const current = list.findIndex((element) => element.closest("tr") === row);
    let next: number | null = null;
    if (event.key === "ArrowDown") next = current + 1;
    else if (event.key === "ArrowUp") next = current - 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = list.length - 1;
    else if (event.key === "Enter" && current !== -1 && event.target !== list[current] && !(event.target as HTMLElement).closest(INTERACTIVE)) {
      // Enter anywhere on a row (not on its own controls) opens it.
      event.preventDefault();
      list[current].click();
      return;
    }
    if (next === null) return;
    event.preventDefault();
    focusRow(next);
  };

  const onRowClick = (event: React.MouseEvent<HTMLTableRowElement>) => {
    // Clicks inside a portaled dialog or popover (text, backdrop) bubble here through React too.
    if (!event.currentTarget.contains(event.target as Node)) return;
    if (event.defaultPrevented || event.button !== 0) return;
    const target = event.target as HTMLElement;
    if (target.closest(INTERACTIVE)) return;
    if (typeof window !== "undefined" && window.getSelection?.()?.toString()) return; // selecting text
    const primary = event.currentTarget.querySelector<HTMLElement>("[data-datalist-primary]");
    if (!primary) return;
    primary.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, metaKey: event.metaKey, ctrlKey: event.ctrlKey, shiftKey: event.shiftKey, altKey: event.altKey, button: 0 }),
    );
  };

  const rowClickable = Boolean(getRowHref || onRowOpen);
  const showSearch = hasSearch;
  const rowHeight = density === "compact" ? "h-row-compact" : "h-row";

  let state: "loading" | "error" | "empty" | "no-match" | "rows";
  if (loading && rows.length === 0) state = "loading";
  else if (error) state = "error";
  else if (rows.length === 0 && !trimmedQuery) state = "empty";
  else if (visibleRows.length === 0) state = "no-match";
  else state = "rows";

  const renderPrimary = (row: Row, content: React.ReactNode) => {
    const rowLabel = getRowLabel?.(row);
    const primaryClass = "rounded-sm font-medium text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring";
    if (getRowHref) {
      return (
        <Link href={getRowHref(row)} data-datalist-primary="" aria-label={rowLabel} className={cn(primaryClass, "hover:underline")}>
          {content}
        </Link>
      );
    }
    if (onRowOpen) {
      return (
        <button type="button" data-datalist-primary="" aria-label={rowLabel} onClick={() => onRowOpen(row)} className={cn(primaryClass, "text-left")}>
          {content}
        </button>
      );
    }
    return content;
  };

  // One live region, mounted from the first render so its text changes are announced.
  const announcement = state === "loading" ? `Loading ${label.toLowerCase()}…` : state === "no-match" ? noMatchText : "";

  return (
    <div data-testid={testId} data-state={state} className={cn("flex min-w-0 flex-col gap-3", className)}>
      <span role="status" aria-live="polite" className="sr-only" data-testid="datalist-status">{announcement}</span>
      {showSearch || toolbar ? (
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {showSearch && searchPlaceholder !== undefined ? (
            <DataListSearch
              ref={searchRef}
              placeholder={searchPlaceholder}
              label={searchLabel ?? searchPlaceholder}
              initialQuery={initialQuery}
              shortcut={searchShortcut}
              onQueryChange={handleQueryChange}
              onArrowDown={focusFirstRow}
            />
          ) : null}
          {toolbar ? <div className="flex shrink-0 flex-wrap items-center gap-2">{toolbar}</div> : null}
        </div>
      ) : null}

      {state === "error" ? (
        <div role="alert" data-testid="datalist-error" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">
          <span className="min-w-0">{error}</span>
          {onRetry ? <Button size="sm" onClick={onRetry}>Retry</Button> : null}
        </div>
      ) : state === "empty" ? (
        <div data-testid="datalist-empty" className="rounded-lg border border-border bg-surface">
          <EmptyState compact title={empty?.title ?? "Nothing here yet"} description={empty?.description} action={empty?.action} />
        </div>
      ) : (
        <div className="min-w-0 overflow-x-auto rounded-lg border border-border bg-surface">
          <table aria-label={label} aria-busy={loading || undefined} className="w-full border-collapse text-label">
            <thead>
              <tr className="h-table-header border-b border-border">
                {columns.map((column) => {
                  const sorted = sort?.columnId === column.id ? sort.direction : null;
                  const ariaSort = column.sortable ? (sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none") : undefined;
                  return (
                    <th
                      key={column.id}
                      scope="col"
                      aria-sort={ariaSort}
                      className={cn("px-3 text-caption font-medium text-muted-foreground", column.align === "end" ? "text-right" : "text-left", column.className, column.headerClassName)}
                    >
                      {column.sortable ? (
                        <button
                          type="button"
                          onClick={() => toggleSort(column.id)}
                          className={cn("-mx-1 inline-flex items-center gap-1 rounded-sm px-1 py-0.5 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring outline-none", sorted && "text-foreground")}
                        >
                          {column.header}
                          {sorted === "asc" ? (
                            <ArrowUp className="size-3" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                          ) : sorted === "desc" ? (
                            <ArrowDown className="size-3" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                          ) : (
                            <ChevronsUpDown className="size-3 opacity-50" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                          )}
                        </button>
                      ) : (
                        column.header
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody ref={bodyRef} onKeyDown={onBodyKeyDown}>
              {state === "loading" ? (
                Array.from({ length: 5 }, (_, index) => (
                  <tr key={index} className={cn(rowHeight, "border-b border-border last:border-b-0")} data-testid="datalist-skeleton-row">
                    {columns.map((column) => (
                      <td key={column.id} className={cn("px-3", column.className)}>
                        <Skeleton className="h-3 w-3/4" />
                      </td>
                    ))}
                  </tr>
                ))
              ) : state === "no-match" ? (
                <tr>
                  <td colSpan={columns.length} className="px-3 py-8 text-center text-label text-muted-foreground" data-testid="datalist-no-match">
                    <span>{noMatchText}</span>{" "}
                    {trimmedQuery ? (
                      <Button
                        variant="link"
                        size="sm"
                        aria-label="Clear search and show all rows"
                        onClick={() => {
                          searchRef.current?.clear();
                          searchRef.current?.focus();
                        }}
                      >
                        Clear search
                      </Button>
                    ) : null}
                  </td>
                </tr>
              ) : (
                visibleRows.map((row) => {
                  const id = getRowId(row);
                  const selected = selectedRowId !== null && selectedRowId === id;
                  return (
                    <tr
                      key={id}
                      data-row-id={id}
                      data-testid={getRowTestId?.(row)}
                      data-selected={selected || undefined}
                      aria-current={selected ? "true" : undefined}
                      onClick={rowClickable ? onRowClick : undefined}
                      className={cn(
                        rowHeight,
                        "border-b border-border last:border-b-0 focus-within:bg-fill-hover",
                        rowClickable && "cursor-pointer hover:bg-fill-hover",
                        selected && "bg-fill-selected hover:bg-fill-selected",
                      )}
                    >
                      {columns.map((column, index) => {
                        const content = column.cell(row);
                        return (
                          <td key={column.id} className={cn("min-w-0 px-3 py-1.5 align-middle text-foreground", column.align === "end" && "text-right tabular-nums", column.className)}>
                            {index === 0 ? renderPrimary(row, content) : content}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
