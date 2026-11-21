"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api";
import { mcpCatalog, networkSummary, type CatalogEntry, type CatalogSource } from "@/lib/mcpCatalog";
import { Select } from "@/components/ui/Select";

const PAGE_SIZE = 30;

type SourceFilter = CatalogSource | "all";

export function toolCountLabel(count: number): string {
  return `${count} ${count === 1 ? "tool" : "tools"}`;
}

function describe(reason: unknown): string {
  if (reason instanceof ApiError) return reason.message;
  return reason instanceof Error ? reason.message : "The catalog could not be loaded";
}

export function registryStatusVariant(status: string | null): "success" | "warning" | "neutral" {
  if (!status) return "neutral";
  if (status === "active") return "success";
  return "warning";
}

interface CatalogListProps {
  onOpen: (entry: CatalogEntry) => void;
  /** Search debounce; tests shorten it. */
  debounceMs?: number;
  /** Focus the search box on mount (the Catalog tab, reached from "Add server"). */
  autoFocusSearch?: boolean;
}

/** The searchable catalog: one row per entry, Load more by cursor. */
export function CatalogList({ onOpen, debounceMs = 300, autoFocusSearch = false }: CatalogListProps) {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<SourceFilter>("toolhive");
  const [items, setItems] = useState<CatalogEntry[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);
  const requestId = useRef(0);
  const first = useRef(true);

  useEffect(() => {
    const id = ++requestId.current;
    const run = async () => {
      setLoading(true);
      setError(null);
      try {
        const page = await mcpCatalog.list({ q: query, limit: PAGE_SIZE, source: source === "all" ? undefined : source });
        if (id !== requestId.current) return;
        setItems(page.items);
        setNext(page.next_after);
      } catch (reason: unknown) {
        if (id === requestId.current) setError(describe(reason));
      } finally {
        if (id === requestId.current) setLoading(false);
      }
    };
    if (first.current) {
      first.current = false;
      void run();
      return () => {
        requestId.current += 1;
      };
    }
    const timer = setTimeout(() => void run(), debounceMs);
    return () => {
      clearTimeout(timer);
      requestId.current += 1;
    };
  }, [query, source, debounceMs, reloadTick]);

  const loadMore = useCallback(async () => {
    if (!next) return;
    const id = requestId.current;
    setLoadingMore(true);
    setError(null);
    try {
      const page = await mcpCatalog.list({ q: query, limit: PAGE_SIZE, after: next, source: source === "all" ? undefined : source });
      if (id !== requestId.current) return;
      setItems((current) => [...current, ...page.items]);
      setNext(page.next_after);
    } catch (reason: unknown) {
      if (id === requestId.current) setError(describe(reason));
    } finally {
      setLoadingMore(false);
    }
  }, [next, query, source]);

  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="mcp-catalog">
      <div className="flex min-w-0 flex-wrap gap-2">
      <Select
        aria-label="Source"
        value={source}
        onValueChange={(v) => setSource(v as SourceFilter)}
        className="rounded-md border border-input bg-background px-3 py-2 text-body text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <option value="toolhive">ToolHive</option>
        <option value="official">Official registry</option>
        <option value="all">All sources</option>
      </Select>
      <input
        type="search"
        autoFocus={autoFocusSearch}
        aria-label="Search the catalog"
        placeholder="Search servers by name or description"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        className="min-w-0 flex-1 rounded-md border border-input bg-background px-3 py-2 text-body text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      </div>

      {error ? (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">
          <span className="min-w-0">{error}</span>
          <Button size="sm" onClick={() => setReloadTick((tick) => tick + 1)}>Retry</Button>
        </div>
      ) : null}

      {loading ? <p role="status" className="text-body text-muted-foreground">Loading the catalog…</p> : null}
      {!loading && !error && items.length === 0 ? (
        <p role="status" className="text-body text-muted-foreground">{query.trim() ? "No servers match that search." : "The catalog is empty."}</p>
      ) : null}

      {items.length > 0 ? (
        <ul aria-label="Catalog entries" className="divide-y divide-border rounded-lg border border-border bg-surface">
          {items.map((entry) => (
            <li key={entry.name}>
              <button
                type="button"
                onClick={() => onOpen(entry)}
                data-testid="mcp-catalog-entry"
                className="flex w-full min-w-0 flex-col gap-1 px-4 py-3 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
              >
                <span className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="truncate font-medium text-foreground">{entry.title || entry.name}</span>
                  {entry.source === "toolhive" ? <Badge variant="info">ToolHive</Badge> : null}
                  {entry.source === "toolhive" && entry.tier ? <Badge variant="outline">{entry.tier}</Badge> : null}
                  {entry.local ? <Badge variant="info">Local</Badge> : null}
                  {entry.remote ? <Badge variant="outline">Remote</Badge> : null}
                  {entry.status ? <Badge variant={registryStatusVariant(entry.status)}>{entry.status}</Badge> : null}
                  {entry.licence ? <span className="text-caption text-muted-foreground">{entry.licence}</span> : null}
                </span>
                <span className="font-mono text-caption text-muted-foreground">{entry.name}</span>
                {entry.source === "toolhive" ? (
                  <span className="flex flex-wrap items-center gap-x-3 text-caption text-muted-foreground">
                    <span>{toolCountLabel(entry.tool_count)}</span>
                    {networkSummary(entry.network) ? <span>{networkSummary(entry.network)}</span> : null}
                  </span>
                ) : null}
                {entry.description ? <span className="line-clamp-2 text-body text-muted-foreground">{entry.description}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      {next ? (
        <div className="flex justify-center">
          <Button disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "Loading…" : "Load more"}</Button>
        </div>
      ) : null}
    </div>
  );
}
