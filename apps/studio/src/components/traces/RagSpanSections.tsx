"use client";

import React from "react";
import Link from "next/link";
import { pageLabel, sourcePages } from "@/components/chat/citations";
import { formatScore, formatTokens } from "@/lib/format";
import { routes } from "@/lib/routes";
import { isSafeHttpUrl } from "@/lib/safeUrl";
import type { NameMaps, RetrieveHit, RetrieveKnowledgeBase, SpanKind, TraceSpan } from "@/lib/traces";

// RM.19: the detail sections of the RAG and memory spans. They read the span's allowlisted
// attributes only (ids, counts, scores, model and usage): the API never puts memory or chunk text
// in them, and nothing here would show it if it did. A retrieve span's query and result stay in the
// span's Input and Output blocks, exactly as for any tool call.

/** Attributes each section shows, left out of the generic attribute list. */
export const RAG_SHOWN: Partial<Record<SpanKind, readonly string[]>> = {
  retrieve: ["query", "knowledge_base", "top_k", "hits", "hit_count", "knowledge_bases"],
  embedding: ["dimensions", "inputs", "input_type", "requests", "batch", "chunks", "embedded", "total", "filled", "written", "approx_tokens", "phase", "stage", "usage_measured"],
  memory_recall: ["dimensions", "inputs", "input_type", "requests", "stage", "hits", "turns", "groups", "messages"],
  memory_index: ["dimensions", "inputs", "input_type", "requests", "stage", "turn", "inserted", "existing", "redacted"],
  observer: ["memory_kind", "covers_from_turn", "covers_to_turn", "approx_tokens", "notes", "redacted", "superseded", "stage"],
  reflector: ["memory_kind", "covers_from_turn", "covers_to_turn", "approx_tokens", "notes", "redacted", "superseded", "stage"],
  working_memory: ["scope", "format", "version", "changed_keys", "other_changed_keys", "redacted"],
  ingest: ["phase", "generation", "embedded", "total", "pending_tokens", "document_id", "doc_version"],
};

/** Kinds whose usage (model, tokens, cost) is shown in the "Model and usage" section. */
export const USAGE_KINDS: ReadonlySet<SpanKind> = new Set<SpanKind>([
  "model_request", "embedding", "memory_recall", "memory_index", "observer", "reflector",
]);

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function Row({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="flex justify-between gap-4 py-0.5" data-testid={testId}>
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-right font-mono text-card-foreground">{children}</dd>
    </div>
  );
}

function Section({ title, testId, children }: { title: string; testId: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} data-testid={testId}>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
      <dl className="mt-1 text-sm">{children}</dl>
    </section>
  );
}

/** A count row, left out when the attribute is absent. */
function Count({ label, value, testId }: { label: string; value: unknown; testId?: string }) {
  const n = num(value);
  return n === null ? null : <Row label={label} testId={testId}>{formatTokens(n)}</Row>;
}

/** A retrieval hit's score: three decimals (the shared formatScore). */
const hitScore = (score: unknown) => formatScore(score, 3);

function kbName(id: string | null | undefined, names: NameMaps | undefined): string | null {
  if (!id) return null;
  return names?.knowledgeBaseNames?.[id] ?? null;
}

function RetrieveSection({ span, names }: { span: TraceSpan; names?: NameMaps }) {
  const a = span.attributes;
  const hits = (Array.isArray(a.hits) ? a.hits : []).filter((h): h is RetrieveHit => !!h && typeof h === "object");
  const kbs = (Array.isArray(a.knowledge_bases) ? a.knowledge_bases : []).filter(
    (k): k is RetrieveKnowledgeBase => !!k && typeof k === "object" && typeof (k as RetrieveKnowledgeBase).id === "string",
  );
  const topK = num(a.top_k);
  return (
    <>
      <Section title="Retrieval" testId="span-retrieve">
        <Row label="Query" testId="span-retrieve-query">{str(a.query) ?? "-"}</Row>
        {str(a.knowledge_base) ? <Row label="Knowledge base">{a.knowledge_base as string}</Row> : null}
        <Row label="Top k" testId="span-retrieve-top-k">{topK === null ? (kbs.length > 1 ? "per knowledge base" : "-") : topK}</Row>
        <Row label="Hits" testId="span-retrieve-hit-count">{hits.length}</Row>
      </Section>
      {kbs.length ? (
        <Section title="Attached knowledge bases" testId="span-retrieve-kbs">
          {kbs.map((kb) => (
            <Row key={kb.id} label={kbName(kb.id, names) ?? kb.id}>
              {`top ${kb.top_k ?? "-"}${kb.min_score != null ? `, min score ${kb.min_score}` : ""}`}
            </Row>
          ))}
        </Section>
      ) : null}
      {hits.length ? (
        <section aria-label="Retrieved sources" data-testid="span-retrieve-hits">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Sources</h4>
          <ol className="mt-1 space-y-1.5 text-sm">
            {hits.map((hit, i) => {
              const title = str(hit.title) ?? str(hit.document_key) ?? "Untitled section";
              const url = isSafeHttpUrl(hit.url) ? hit.url : null;
              const name = kbName(hit.kb_id, names);
              // A PDF section's page: the hit's `page`/`page_end` when the span carries them, else its URL's `#page=N`.
              const page = pageLabel(sourcePages(hit as RetrieveHit & { page?: unknown; page_end?: unknown }));
              return (
                <li key={str(hit.chunk_id) ?? i} className="min-w-0" data-testid="span-retrieve-hit">
                  <div className="flex min-w-0 items-baseline justify-between gap-3">
                    <span className="min-w-0 break-words">
                      <span className="font-mono text-muted-foreground">[{num(hit.n) ?? i + 1}]</span>{" "}
                      {url ? (
                        <a
                          href={url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-info-text underline-offset-4 hover:underline"
                          data-testid="span-retrieve-hit-link"
                        >
                          {title}
                        </a>
                      ) : (
                        <span className="text-card-foreground">{title}</span>
                      )}
                    </span>
                    <span className="shrink-0 font-mono text-xs text-muted-foreground" data-testid="span-retrieve-hit-score">
                      {hitScore(hit.score)}
                    </span>
                  </div>
                  <p className="break-all font-mono text-xs text-muted-foreground" data-testid="span-retrieve-hit-meta" data-page={page ?? undefined}>
                    {[
                      !url && str(hit.ref) ? hit.ref : null,
                      page,
                      name,
                      hit.doc_version != null ? `v${hit.doc_version}` : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                </li>
              );
            })}
          </ol>
        </section>
      ) : null}
    </>
  );
}

function EmbeddingSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const title = span.kind === "memory_recall" ? "Semantic recall" : span.kind === "memory_index" ? "Memory index" : "Embedding";
  return (
    <Section title={title} testId="span-embedding">
      <Count label="Dimensions" value={a.dimensions} testId="span-embedding-dimensions" />
      <Count label="Inputs" value={a.inputs} testId="span-embedding-inputs" />
      {str(a.input_type) ? <Row label="Input type">{a.input_type as string}</Row> : null}
      <Count label="Provider requests" value={a.requests} />
      <Count label="Batch" value={a.batch} />
      <Count label="Chunks" value={a.chunks} />
      {num(a.embedded) !== null && num(a.total) !== null ? (
        <Row label="Embedded">{`${formatTokens(num(a.embedded))} of ${formatTokens(num(a.total))}`}</Row>
      ) : null}
      {span.kind === "memory_recall" ? (
        <>
          <Count label="Hits" value={a.hits} testId="span-recall-hits" />
          <Count label="Turns" value={a.turns} />
          <Count label="Groups" value={a.groups} />
          <Count label="Messages added" value={a.messages} />
        </>
      ) : null}
      {span.kind === "memory_index" ? (
        <>
          <Count label="Turn" value={a.turn} />
          <Count label="Inserted" value={a.inserted} />
          <Count label="Already indexed" value={a.existing} />
          <Count label="Redacted" value={a.redacted} />
        </>
      ) : null}
    </Section>
  );
}

function ObserverSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const from = num(a.covers_from_turn);
  const to = num(a.covers_to_turn);
  return (
    <Section title={span.kind === "reflector" ? "Reflection" : "Observation"} testId="span-observer">
      {str(a.memory_kind) ? <Row label="Memory kind">{a.memory_kind as string}</Row> : null}
      {from !== null && to !== null ? <Row label="Covers turns">{`${from} to ${to}`}</Row> : null}
      <Count label="Notes" value={a.notes} />
      <Count label="Approx. tokens" value={a.approx_tokens} />
      <Count label="Superseded" value={a.superseded} />
      <Count label="Redacted" value={a.redacted} />
    </Section>
  );
}

function WorkingMemorySection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const keys = Array.isArray(a.changed_keys) ? a.changed_keys.filter((k): k is string => typeof k === "string") : [];
  const more = num(a.other_changed_keys);
  return (
    <Section title="Working memory" testId="span-working-memory">
      {str(a.scope) ? <Row label="Scope">{a.scope as string}</Row> : null}
      {str(a.format) ? <Row label="Format">{a.format as string}</Row> : null}
      <Count label="Version" value={a.version} />
      {keys.length || more ? (
        <Row label="Changed keys">{`${keys.join(", ")}${more ? ` (+${more} more)` : ""}`}</Row>
      ) : null}
      <Count label="Redacted" value={a.redacted} />
    </Section>
  );
}

function IngestSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  return (
    <Section title="Ingest phase" testId="span-ingest">
      {str(a.phase) ? <Row label="Phase">{a.phase as string}</Row> : null}
      <Count label="Generation" value={a.generation} />
      {num(a.embedded) !== null && num(a.total) !== null ? (
        <Row label="Embedded">{`${formatTokens(num(a.embedded))} of ${formatTokens(num(a.total))}`}</Row>
      ) : null}
      <Count label="Tokens to embed" value={a.pending_tokens} />
      {str(a.document_id) ? <Row label="Document">{`${a.document_id as string}${a.doc_version != null ? ` v${a.doc_version}` : ""}`}</Row> : null}
    </Section>
  );
}

/** The kind-specific section of a RAG or memory span, or null for other kinds. */
export function RagSpanSection({ span, names }: { span: TraceSpan; names?: NameMaps }) {
  switch (span.kind) {
    case "retrieve":
      return <RetrieveSection span={span} names={names} />;
    case "embedding":
    case "memory_recall":
    case "memory_index":
      return <EmbeddingSection span={span} />;
    case "observer":
    case "reflector":
      return <ObserverSection span={span} />;
    case "working_memory":
      return <WorkingMemorySection span={span} />;
    case "ingest":
      return <IngestSection span={span} />;
    default:
      return null;
  }
}

/** "Open knowledge base" for a kb_ingest run span (its Ingest runs tab). */
export function KnowledgeBaseLink({ span, names }: { span: TraceSpan; names?: NameMaps }) {
  const id = str(span.attributes.knowledge_base_id);
  if (!id || (span.kind !== "run" && span.kind !== "child_run")) return null;
  const name = kbName(id, names);
  return (
    <Link
      href={routes.knowledgeBase(id, { tab: "runs" })}
      data-testid="span-link-knowledge-base"
      className="text-primary underline-offset-4 hover:underline"
    >
      {name ? `Open knowledge base: ${name}` : "Open knowledge base"}
    </Link>
  );
}
