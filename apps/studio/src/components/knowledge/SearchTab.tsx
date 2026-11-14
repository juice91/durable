"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { TraceLink } from "@/components/traces/TraceLink";
import { EmptyState } from "@/components/shared/EmptyState";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { isSafeHttpUrl, parseFilter, searchKnowledgeBase, type SearchHit, type SearchOutcome } from "@/lib/knowledge";
import { routes } from "@/lib/routes";
import { alertClass, fieldClass, hintClass, labelClass } from "./knowledgeUi";

/** Characters of a section shown per result. */
export const SNIPPET_CHARS = 600;

function headingPath(hit: SearchHit): string | null {
  if (Array.isArray(hit.heading_path)) return hit.heading_path.filter(Boolean).join(" › ") || null;
  return typeof hit.heading_path === "string" && hit.heading_path ? hit.heading_path : null;
}

/**
 * The search tester: the same retrieval an agent's `retrieve` tool runs (a priced tool run of
 * `knowledge.search`), with the query, top_k, min score and a metadata filter. Results show the
 * score, title, anchor link and the section text.
 */
export function SearchTab({ kbId }: { kbId: string }) {
  const id = useId();
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState("8");
  const [minScore, setMinScore] = useState("");
  const [filterText, setFilterText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<SearchOutcome | null>(null);
  const abort = useRef<AbortController | null>(null);
  // Leaving the tab stops the request and its polling of the tool run.
  useEffect(() => () => abort.current?.abort(), []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!query.trim()) {
      setError("Type a query.");
      return;
    }
    const k = Number(topK);
    if (!Number.isInteger(k) || k < 1 || k > 20) {
      setError("top_k must be a whole number from 1 to 20.");
      return;
    }
    const min = minScore.trim() === "" ? null : Number(minScore);
    if (min !== null && (!Number.isFinite(min) || min < -1 || min > 1)) {
      setError("Min score must be a number from -1 to 1.");
      return;
    }
    const filter = parseFilter(filterText);
    if (!filter.ok) {
      setError(filter.reason);
      return;
    }
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError(null);
    try {
      const result = await searchKnowledgeBase(kbId, { query: query.trim(), top_k: k, min_score: min, filter: filter.value }, { signal: controller.signal });
      if (abort.current === controller) setOutcome(result);
    } catch (reason: unknown) {
      if (abort.current === controller && !(reason instanceof DOMException && reason.name === "AbortError")) {
        setError(reason instanceof Error ? reason.message : "The search failed");
      }
    } finally {
      if (abort.current === controller && !controller.signal.aborted) setBusy(false);
    }
  };

  const hits = outcome?.status === "completed" ? outcome.result.hits ?? [] : [];

  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="knowledge-search-tab">
      <form onSubmit={(event) => void submit(event)} className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-4" aria-label="Search tester" data-testid="knowledge-search-form">
        <div className="min-w-0 space-y-1">
          <label htmlFor={`${id}-query`} className={labelClass}>Query</label>
          <input id={`${id}-query`} data-testid="knowledge-search-query" value={query} onChange={(event) => setQuery(event.target.value)} className={fieldClass} placeholder="How do I reset my password?" />
        </div>
        <div className="grid min-w-0 grid-cols-[repeat(auto-fit,minmax(min(100%,10rem),1fr))] gap-3">
          <div className="min-w-0 space-y-1">
            <label htmlFor={`${id}-k`} className={labelClass}>top_k</label>
            <input id={`${id}-k`} data-testid="knowledge-search-top-k" type="number" min={1} max={20} step={1} value={topK} onChange={(event) => setTopK(event.target.value)} className={fieldClass} />
          </div>
          <div className="min-w-0 space-y-1">
            <label htmlFor={`${id}-min`} className={labelClass}>Min score</label>
            <input id={`${id}-min`} data-testid="knowledge-search-min-score" type="number" min={-1} max={1} step={0.01} value={minScore} onChange={(event) => setMinScore(event.target.value)} className={fieldClass} placeholder="none" />
          </div>
        </div>
        <div className="min-w-0 space-y-1">
          <label htmlFor={`${id}-filter`} className={labelClass}>Metadata filter <span className="font-normal text-muted-foreground">(optional)</span></label>
          <textarea id={`${id}-filter`} data-testid="knowledge-search-filter" rows={2} value={filterText} onChange={(event) => setFilterText(event.target.value)} className={`${fieldClass} font-mono`} placeholder='{"lang": "en"}' />
          <p className={hintClass}>A JSON object; a list matches any of its values. The same filter an agent&apos;s Knowledge step sets.</p>
        </div>
        {error ? <p role="alert" data-testid="knowledge-search-error" className={alertClass}>{error}</p> : null}
        <div className="flex flex-wrap items-center justify-end gap-2">
          {busy ? <span className="text-caption text-muted-foreground" aria-live="polite">Searching…</span> : null}
          <Button type="submit" variant="primary" disabled={busy} data-testid="knowledge-search-submit">Search</Button>
        </div>
      </form>

      {outcome ? (
        <section aria-labelledby={`${id}-results`} className="flex min-w-0 flex-col gap-3" data-testid="knowledge-search-results" data-status={outcome.status}>
          <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
            <h2 id={`${id}-results`} className="text-title font-semibold text-foreground">
              {outcome.status === "completed" ? `${hits.length} result${hits.length === 1 ? "" : "s"}` : "Search failed"}
            </h2>
            <TraceLink runId={outcome.runId} className="text-caption text-info-text hover:underline" data-testid="knowledge-search-trace">
              Open trace
            </TraceLink>
          </div>
          {outcome.status === "failed" ? <p role="alert" className={alertClass}>{outcome.error}</p> : null}
          {outcome.status === "completed" && hits.length === 0 ? (
            <EmptyState compact testId="knowledge-search-empty" title="No sections matched" description="Try other words, a lower min score, or no filter." />
          ) : null}
          <ol className="flex min-w-0 flex-col gap-3">
            {hits.map((hit) => {
              const path = headingPath(hit);
              const text = hit.content ?? "";
              return (
                <li key={`${hit.chunk_id}-${hit.n}`} data-testid="knowledge-search-result" className="min-w-0 rounded-lg border border-border bg-surface p-3">
                  <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span className="font-mono text-caption text-muted-foreground">[{hit.n}]</span>
                    <span className="min-w-0 flex-1 truncate font-medium text-foreground">{hit.title}</span>
                    <Badge variant="info" data-testid="knowledge-search-score" title="Similarity score">{hit.score.toFixed(3)}</Badge>
                  </div>
                  {path ? <p className="mt-1 truncate text-caption text-muted-foreground">{path}</p> : null}
                  <p className="mt-1 min-w-0 break-all text-caption">
                    {isSafeHttpUrl(hit.url) ? (
                      <a href={hit.url} target="_blank" rel="noopener noreferrer nofollow" data-testid="knowledge-search-url" className="text-info-text underline-offset-4 hover:underline">{hit.url}</a>
                    ) : (
                      <Link href={routes.knowledgeBase(kbId, { document: hit.document_key })} data-testid="knowledge-search-document" className="font-mono text-info-text underline-offset-4 hover:underline">
                        {hit.ref || `kb:${hit.document_key}`}
                      </Link>
                    )}
                  </p>
                  {text ? (
                    <p className="mt-2 line-clamp-6 min-w-0 whitespace-pre-wrap break-words text-label text-foreground">
                      {text.slice(0, SNIPPET_CHARS)}{text.length > SNIPPET_CHARS || hit.content_truncated ? "…" : ""}
                    </p>
                  ) : null}
                  {hit.metadata && Object.keys(hit.metadata).length > 0 ? (
                    <div className="mt-2 flex min-w-0 flex-wrap gap-1">
                      {Object.entries(hit.metadata).map(([key, value]) => <Badge key={key} variant="neutral" className="font-mono">{key}: {String(value)}</Badge>)}
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ol>
        </section>
      ) : null}
    </div>
  );
}
