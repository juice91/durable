// Knowledge base client (/api/v1/knowledge-bases, /api/v1/embedding-models, and the knowledge base's
// runs on /api/v1/runs) and the pure helpers the Knowledge pages use (RM.16).
//
// Uploads are JSON: Studio maps each file's extension to its MIME type through UPLOAD_FORMATS (the
// API's format registry, eap_knowledge.formats). Text formats are read as strict UTF-8 and sent as
// `content`; binary formats (PDF) are read as bytes and sent as `content_base64` (RAG P1 decision
// "Upload contract"). The API is the authority on caps and content sniffing (413/415/422).
import { apiErrorFromResponse, apiHeaders, ApiError, BASE_URL, type AgentSpec } from "./api";
import type { Run, RunEvent } from "./runs";

// ---------------------------------------------------------------------------------------------
// Shapes (apps/api/src/eap_api/schemas.py)

export type KnowledgeBaseStatus = "ready" | "reindexing" | "reindex_failed";

/** Heading detection for PDFs (`ExtractConfig.pdf_headings`). */
export type PdfHeadings = "outline" | "font" | "pages";
export type MarkdownFlavor = "plain" | "doxygen";

/**
 * A knowledge base's extraction options (`chunking.extract`, eap_schemas.knowledge.ExtractConfig):
 * every field is left out while unset. `pdf_images` is part 2 and not offered here yet.
 */
export interface ExtractConfig {
  rst_config?: Record<string, MetadataValue>;
  rst_tags?: string[];
  pdf_headings?: PdfHeadings;
  pdf_images?: "off" | "embedded" | "pages";
  markdown_flavor?: MarkdownFlavor;
}

export interface ChunkingConfig {
  max_tokens: number;
  overlap_tokens: number;
  min_tokens: number;
  chunker_version?: number;
  extract?: ExtractConfig;
}

/** `chunking` of POST /knowledge-bases: every field optional (the API fills in the defaults; new
 * knowledge bases get chunker v2). */
export interface ChunkingCreate {
  max_tokens?: number;
  overlap_tokens?: number;
  min_tokens?: number;
  chunker_version?: number;
  extract?: ExtractConfig;
}

export interface KnowledgeBase {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  embedding_model: string;
  dimensions: number;
  chunking: ChunkingConfig | Record<string, unknown>;
  active_generation: number;
  last_generation: number;
  pending_generation: number | null;
  pending_embedding_model: string | null;
  pending_dimensions: number | null;
  status: KnowledgeBaseStatus;
  error: string | null;
  created_at: string;
  updated_at: string;
  document_count?: number | null;
  chunk_count?: number | null;
}

/** GET /knowledge-bases/{id}: plus what its kb_ingest runs spent. `ingest_cost_usd` is a decimal
 * string, null when any of those requests had no price. */
export interface KnowledgeBaseDetail extends KnowledgeBase {
  ingest_tokens: number;
  ingest_cost_usd: string | null;
  ingest_requests: number;
}

export type DocumentStatus = "pending" | "ingesting" | "ready" | "failed";

export type MetadataValue = string | number | boolean;

export interface KnowledgeDocument {
  id: string;
  tenant_id: string;
  kb_id: string;
  document_key: string;
  title: string;
  source_url: string | null;
  mime_type: string;
  metadata: Record<string, MetadataValue>;
  content_hash: string;
  active_version: number | null;
  latest_version: number;
  status: DocumentStatus;
  error: string | null;
  chunk_count: number;
  last_run_id: string | null;
  created_at: string;
  updated_at: string;
  content?: string | null;
  /** "base64" for a binary document (a PDF): its `content` is always null (the bytes never leave). */
  content_encoding?: "base64" | null;
  /** From `error`: the ingest failure code, and for an extraction failure its extraction code. */
  error_code?: string | null;
  error_reason?: string | null;
  /** GET of one document only: the worker extraction of its active version (null for API-extracted
   * documents, or while no version is active). */
  extraction?: DocumentExtraction | null;
}

/** The `extracted` event of a document's active version (counts only, never text). */
export interface DocumentExtraction {
  extractor: string;
  extractor_version: number;
  /** The extractor's current version; a higher one than `extractor_version` means Re-extract would use a newer extractor. */
  current_version?: number | null;
  doc_version?: number | null;
  run_id?: string | null;
  sections?: number | null;
  chars?: number | null;
  pages?: number | null;
  image_only_pages?: number[] | null;
  conditions?: number | null;
  warnings?: number | null;
}

export interface DocumentPage {
  items: KnowledgeDocument[];
  /** Opaque cursor for the next page (pass back verbatim as `after`), or null on the last page. */
  next_after: string | null;
}

export interface EmbeddingModel {
  id: string;
  provider: string;
  name: string;
  dimensions: number;
  /** `provider:*`: any model name of that provider at these dimensions. */
  wildcard: boolean;
}

export interface KnowledgeBaseCreate {
  name: string;
  description?: string | null;
  embedding_model: string;
  chunking?: ChunkingCreate | null;
}

/** PUT …/documents/{key}: exactly one of `content` (text formats) and `content_base64` (binary). */
export interface DocumentPut {
  title?: string | null;
  content?: string;
  content_base64?: string;
  mime_type: string;
  source_url?: string | null;
  metadata?: Record<string, MetadataValue> | null;
  run_id?: string | null;
}

/** PUT …/documents/{key}: `unchanged` true means nothing was written and no run started. */
export interface DocumentPutResult {
  unchanged: boolean;
  created: boolean;
  document: KnowledgeDocument;
  run_id: string | null;
  status?: string | null;
  display_status?: string | null;
  trace_id?: string | null;
}

export interface IngestRunResult {
  run_id: string | null;
  kind: "kb_ingest";
  knowledge_base_id: string;
  status?: string | null;
  display_status?: string | null;
  trace_id?: string | null;
  unchanged?: boolean;
  document?: KnowledgeDocument | null;
  knowledge_base?: KnowledgeBase | null;
}

/** POST /knowledge-bases/{id}/reextract. */
export interface ReextractRequest {
  /** Registry format names (`pdf`, `rst`, …); omitted means every format. */
  formats?: string[];
  /** Default true: only documents extracted by an older extractor version. */
  only_outdated?: boolean;
  /** 2: move the knowledge base to chunker v2 and re-extract every document. */
  chunker_version?: 2;
  dry_run?: boolean;
}

export interface ReextractDocumentRef {
  document_id: string;
  document_key: string;
  mime_type: string;
  doc_version: number;
  run_id: string | null;
  status?: string | null;
  detail?: string | null;
}

export interface ReextractResult {
  knowledge_base_id: string;
  dry_run: boolean;
  chunker_version: number;
  chunker_version_changed: boolean;
  selected: number;
  started: number;
  failed: number;
  documents: ReextractDocumentRef[];
  knowledge_base?: KnowledgeBase | null;
}

export interface ReindexEstimate {
  dry_run: true;
  knowledge_base_id: string;
  embedding_model: string;
  dimensions: number;
  chunks: number;
  approx_tokens: number;
  max_ingest_tokens: number;
  exceeds_budget: boolean;
  /** Decimal string, or null when the API cannot price the model (`cost_note` says why). */
  estimated_cost_usd: string | null;
  price_source: string | null;
  cost_note: string | null;
  reindex_pending: boolean;
}

/** One agent in a delete 409's `agents`: its latest published version, or (`draft`) its draft only. */
export interface KnowledgeBaseReference {
  agent_id: string;
  agent_name: string;
  /** The published version that attaches it; null for a draft-only reference. */
  version: number | null;
  archived: boolean;
  /** The draft attaches it (no published version does). Absent from older APIs: false. */
  draft?: boolean;
}

/** "v3", "v3 (archived)", "(draft)", "(draft, archived)" for a delete 409's reference. */
export function referenceLabel(item: Pick<KnowledgeBaseReference, "version" | "archived" | "draft">): string {
  const archived = item.archived ? "archived" : null;
  if (item.draft || item.version === null) return `(${["draft", archived].filter(Boolean).join(", ")})`;
  return `v${item.version}${archived ? " (archived)" : ""}`;
}

export interface SearchRequest {
  query: string;
  top_k?: number;
  min_score?: number | null;
  filter?: Record<string, MetadataValue | MetadataValue[]> | null;
}

export interface SearchHit {
  n: number;
  title: string;
  url: string | null;
  ref: string;
  kb_id: string;
  document_id: string;
  document_key: string;
  chunk_id: string;
  doc_version: number;
  score: number;
  document_title?: string | null;
  heading_path?: string[] | string | null;
  metadata?: Record<string, MetadataValue> | null;
  content?: string | null;
  content_truncated?: boolean;
}

export interface SearchResult {
  knowledge_base_id: string;
  embedding_model: string;
  hits: SearchHit[];
  text?: string;
}

/** POST …/search, after any wait for the tool run: completed with a result, or failed with an error. */
export type SearchOutcome =
  | { status: "completed"; runId: string; traceId: string | null; result: SearchResult }
  | { status: "failed"; runId: string; traceId: string | null; error: string };

// ---------------------------------------------------------------------------------------------
// Errors

/**
 * 409 of DELETE /knowledge-bases/{id} without force: the agents whose latest version attaches it.
 * A plain Error (with `status` 409), not an ApiError subclass: nothing here may touch `@/lib/api`'s
 * exports while this module loads, because tests that mock that module whole still import this one
 * (through the agent page's Config panel and the palette).
 */
export class KnowledgeBaseInUseError extends Error {
  readonly status = 409;
  constructor(message: string, public agents: KnowledgeBaseReference[]) {
    super(message);
    this.name = "KnowledgeBaseInUseError";
  }
}

// ---------------------------------------------------------------------------------------------
// Requests

const V1 = "/api/v1";

function seg(value: string): string {
  return encodeURIComponent(value);
}

async function send(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(`${BASE_URL}${V1}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...apiHeaders() },
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await send(path, init);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

const kbPath = (id: string) => `/knowledge-bases/${seg(id)}`;
const docPath = (kbId: string, key: string) => `${kbPath(kbId)}/documents/${seg(key)}`;

/** The tenant's knowledge bases by name, with `document_count` and `chunk_count`. */
export async function listKnowledgeBases(init?: RequestInit): Promise<KnowledgeBase[]> {
  const page = await request<{ items: KnowledgeBase[] }>("/knowledge-bases", init);
  return page.items;
}

export function getKnowledgeBase(id: string, init?: RequestInit): Promise<KnowledgeBaseDetail> {
  return request<KnowledgeBaseDetail>(kbPath(id), init);
}

/** 422 model not allowed; 409 a knowledge base of that name exists. */
export function createKnowledgeBase(body: KnowledgeBaseCreate): Promise<KnowledgeBase> {
  return request<KnowledgeBase>("/knowledge-bases", { method: "POST", body: JSON.stringify(body) });
}

export function updateKnowledgeBase(id: string, body: { name?: string; description?: string | null }): Promise<KnowledgeBase> {
  return request<KnowledgeBase>(kbPath(id), { method: "PATCH", body: JSON.stringify(body) });
}

/**
 * Delete a knowledge base (its documents and sections go with it). Without `force`, a knowledge base
 * that an agent's latest published version attaches answers 409: thrown as KnowledgeBaseInUseError
 * with those agents.
 */
export async function deleteKnowledgeBase(id: string, { force = false }: { force?: boolean } = {}): Promise<void> {
  const response = await fetch(`${BASE_URL}${V1}${kbPath(id)}${force ? "?force=true" : ""}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
  });
  if (response.status === 409) {
    let body: { detail?: unknown; agents?: unknown } = {};
    try {
      body = await response.json();
    } catch {
      // A 409 without a JSON body: no agent list.
    }
    if (Array.isArray(body.agents)) {
      throw new KnowledgeBaseInUseError(typeof body.detail === "string" ? body.detail : "The knowledge base is in use", body.agents as KnowledgeBaseReference[]);
    }
    throw new ApiError(409, typeof body.detail === "string" ? body.detail : "HTTP 409");
  }
  if (!response.ok) throw await apiErrorFromResponse(response);
}

export async function listEmbeddingModels(): Promise<EmbeddingModel[]> {
  const page = await request<{ items: EmbeddingModel[] }>("/embedding-models");
  return page.items;
}

/** One page of documents by document_key (`limit` 1–200). */
export function listDocuments(kbId: string, { limit = 50, after = null }: { limit?: number; after?: string | null } = {}): Promise<DocumentPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (after) params.set("after", after);
  return request<DocumentPage>(`${kbPath(kbId)}/documents?${params.toString()}`);
}

export function getDocument(kbId: string, key: string, { includeContent = false, signal }: { includeContent?: boolean; signal?: AbortSignal } = {}): Promise<KnowledgeDocument> {
  return request<KnowledgeDocument>(`${docPath(kbId, key)}${includeContent ? "?include_content=true" : ""}`, { signal });
}

/** The exact JSON body `putDocument` sends (the API caps the request body, then the decoded content). */
export function documentPutJson(key: string, body: DocumentPut): string {
  return JSON.stringify({ document_key: key, ...body });
}

/** The UTF-8 size of that body, which EAP_KB_MAX_DOCUMENT_BYTES limits for text uploads. */
export function documentPutBytes(key: string, body: DocumentPut): number {
  return new TextEncoder().encode(documentPutJson(key, body)).length;
}

/**
 * Why the upload is over a cap (as the API would answer 413), or null. A text upload's JSON body is
 * capped at `maxBytes` (EAP_KB_MAX_DOCUMENT_BYTES), and YAML or JSON content also at the structured
 * cap; a binary upload's body at the request guard (`uploadGuardBytes`) and its decoded bytes at the
 * binary cap. `maxBytes` (a number) keeps the text cap's old meaning; pass `caps` for the others.
 */
export function documentPutProblem(key: string, body: DocumentPut, maxBytes: number = DEFAULT_MAX_DOCUMENT_BYTES, caps: UploadCaps = DEFAULT_UPLOAD_CAPS): string | null {
  const limits: UploadCaps = { ...caps, text: maxBytes };
  const format = uploadFormatForMime(body.mime_type);
  if (body.content_base64 !== undefined) {
    const guard = uploadGuardBytes(limits);
    if (documentPutBase64Bytes(key, body) > guard) return `The upload is larger than ${mibLabel(guard)} MiB once encoded (JSON).`;
    const cap = format ? capFor(format, limits) : limits.binary;
    const decoded = base64DecodedLength(body.content_base64);
    return decoded > cap ? `The file is larger than ${mibLabel(cap)} MiB.` : null;
  }
  if (documentPutBytes(key, body) > limits.text) return `The upload is larger than ${mibLabel(limits.text)} MiB once encoded (JSON).`;
  if (format && format.capClass !== "text") {
    const cap = capFor(format, limits);
    if (new TextEncoder().encode(body.content ?? "").length > cap) return `The file is larger than ${mibLabel(cap)} MiB.`;
  }
  return null;
}

/**
 * `documentPutBytes` of a base64 upload, without serialising its (up to 34 MB) string: base64 is
 * ASCII and needs no JSON escaping, so the body is the envelope with an empty `content_base64` plus
 * the string's length.
 */
export function documentPutBase64Bytes(key: string, body: DocumentPut): number {
  const value = body.content_base64 ?? "";
  return documentPutBytes(key, { ...body, content_base64: "" }) + value.length;
}

/** Upload (or replace) a document. 200 unchanged; 202 stored with an ingest run (`run_id`). */
export function putDocument(kbId: string, key: string, body: DocumentPut, init: RequestInit = {}): Promise<DocumentPutResult> {
  return request<DocumentPutResult>(docPath(kbId, key), { ...init, method: "PUT", body: documentPutJson(key, body) });
}

export function deleteDocument(kbId: string, key: string): Promise<void> {
  return request<void>(docPath(kbId, key), { method: "DELETE" });
}

/**
 * Re-extract: a new version of the document with the same content and an ingest run of it (202),
 * so the current extractor and chunker produce its chunks. `runId` (client-generated) makes a
 * repeated click idempotent: the API answers the same run.
 */
export function reextractDocument(kbId: string, key: string, runId?: string | null): Promise<IngestRunResult> {
  return request<IngestRunResult>(`${docPath(kbId, key)}/reextract`, { method: "POST", body: JSON.stringify(runId ? { run_id: runId } : {}) });
}

/** A UUID v4 for a client-generated `run_id` (crypto.randomUUID, else built from getRandomValues:
 * randomUUID exists only in secure contexts). */
export function newClientRunId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** Bulk re-extract (`dry_run` answers the selection and starts nothing). */
export function reextractKnowledgeBase(kbId: string, body: ReextractRequest): Promise<ReextractResult> {
  return request<ReextractResult>(`${kbPath(kbId)}/reextract`, { method: "POST", body: JSON.stringify(body) });
}

/** Retry: a new ingest run of the document's latest version (200 with `run_id` null when already ingested). */
export function reingestDocument(kbId: string, key: string): Promise<IngestRunResult> {
  return request<IngestRunResult>(`${docPath(kbId, key)}/reingest`, { method: "POST", body: JSON.stringify({}) });
}

/** The dry run of a reindex with `embeddingModel`: what it would embed and cost. Starts nothing. */
export function estimateReindex(kbId: string, embeddingModel: string): Promise<ReindexEstimate> {
  return request<ReindexEstimate>(`${kbPath(kbId)}/reindex`, { method: "POST", body: JSON.stringify({ embedding_model: embeddingModel, dry_run: true }) });
}

export function startReindex(kbId: string, embeddingModel: string): Promise<IngestRunResult> {
  return request<IngestRunResult>(`${kbPath(kbId)}/reindex`, { method: "POST", body: JSON.stringify({ embedding_model: embeddingModel }) });
}

export function cancelReindex(kbId: string): Promise<void> {
  return request<void>(`${kbPath(kbId)}/reindex`, { method: "DELETE" });
}

interface SearchView {
  run_id: string;
  status: string;
  trace_id?: string | null;
  result?: SearchResult | null;
  error?: string | null;
}

function searchOutcome(view: SearchView): SearchOutcome | null {
  const traceId = view.trace_id ?? null;
  if (view.status === "completed") {
    return { status: "completed", runId: view.run_id, traceId, result: view.result ?? { knowledge_base_id: "", embedding_model: "", hits: [] } };
  }
  if (view.status === "failed" || view.status === "needs_attention") {
    return { status: "failed", runId: view.run_id, traceId, error: view.error || "The search failed" };
  }
  return null;
}

/** Resolves after `ms`, or rejects with an AbortError as soon as `signal` aborts. */
function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface SearchOptions {
  /** How long to keep polling GET /runs/{id} after the API's own 15 s wait answered 202. */
  pollTimeoutMs?: number;
  pollIntervalMs?: number;
  signal?: AbortSignal;
}

/**
 * The search tester: POST …/search records a tool run of `knowledge.search` and waits up to 15 s.
 * When it is still running (202), this polls the run until it ends or `pollTimeoutMs` passes.
 */
export async function searchKnowledgeBase(kbId: string, body: SearchRequest, { pollTimeoutMs = 30_000, pollIntervalMs = 1_000, signal }: SearchOptions = {}): Promise<SearchOutcome> {
  const view = await request<SearchView>(`${kbPath(kbId)}/search`, { method: "POST", body: JSON.stringify(body), signal });
  const done = searchOutcome(view);
  if (done) return done;
  const deadline = Date.now() + pollTimeoutMs;
  while (Date.now() < deadline) {
    await abortableSleep(pollIntervalMs, signal);
    const run = await request<Run & { output: string | null }>(`/runs/${seg(view.run_id)}`, { signal });
    if (run.status === "completed") {
      let result: SearchResult | null = null;
      try {
        result = run.output ? (JSON.parse(run.output) as SearchResult) : null;
      } catch {
        result = null;
      }
      return { status: "completed", runId: view.run_id, traceId: run.trace_id ?? null, result: result ?? { knowledge_base_id: kbId, embedding_model: "", hits: [] } };
    }
    if (run.status === "failed" || run.status === "needs_attention") {
      return { status: "failed", runId: view.run_id, traceId: run.trace_id ?? null, error: run.error || "The search failed" };
    }
  }
  return { status: "failed", runId: view.run_id, traceId: view.trace_id ?? null, error: "The search is still running; open its trace to follow it." };
}

/** A knowledge base's runs, newest first: its kb_ingest runs (`kind: "kb_ingest"`), or with no kind
 * also its search tester runs. */
export function listKnowledgeBaseRuns(
  kbId: string,
  { kind = "kb_ingest", limit = 50, before = null }: { kind?: "kb_ingest" | null; limit?: number; before?: string | null } = {},
): Promise<{ items: Run[]; next_before: string | null }> {
  const params = new URLSearchParams({ knowledge_base_id: kbId, limit: String(limit) });
  if (kind) params.set("kind", kind);
  if (before) params.set("before", before);
  return request<{ items: Run[]; next_before: string | null }>(`/runs?${params.toString()}`);
}

// ---------------------------------------------------------------------------------------------
// Uploads

/** Which cap applies to a format (the registry's `cap_class`). */
export type UploadCapClass = "text" | "structured" | "binary";

/** One upload format: the mirror of eap_knowledge.formats.FORMAT_SPECS (the API is the authority). */
export interface UploadFormat {
  /** The registry name (`markdown`, `html`, `text`, `rst`, `pdf`, `yaml`, `json`). */
  format: string;
  label: string;
  mimeType: string;
  aliases: readonly string[];
  extensions: readonly string[];
  /** Sent as `content_base64` (else as `content`, UTF-8 text). */
  binary: boolean;
  capClass: UploadCapClass;
  /** Extracted by the worker (its extractor and version are recorded; Re-extract applies). */
  worker: boolean;
}

export const UPLOAD_FORMATS: readonly UploadFormat[] = [
  { format: "markdown", label: "Markdown", mimeType: "text/markdown", aliases: ["text/x-markdown", "text/md"], extensions: [".md", ".markdown"], binary: false, capClass: "text", worker: false },
  { format: "html", label: "HTML", mimeType: "text/html", aliases: ["application/xhtml+xml"], extensions: [".html", ".htm"], binary: false, capClass: "text", worker: false },
  { format: "text", label: "Plain text", mimeType: "text/plain", aliases: [], extensions: [".txt", ".text"], binary: false, capClass: "text", worker: false },
  { format: "rst", label: "reStructuredText", mimeType: "text/x-rst", aliases: ["text/prs.fallenstein.rst"], extensions: [".rst", ".rest"], binary: false, capClass: "text", worker: true },
  { format: "pdf", label: "PDF", mimeType: "application/pdf", aliases: [], extensions: [".pdf"], binary: true, capClass: "binary", worker: true },
  { format: "yaml", label: "YAML", mimeType: "application/yaml", aliases: ["application/x-yaml", "text/yaml"], extensions: [".yaml", ".yml"], binary: false, capClass: "structured", worker: true },
  { format: "json", label: "JSON", mimeType: "application/json", aliases: [], extensions: [".json"], binary: false, capClass: "structured", worker: true },
];

/** Extension (lower case, with the dot) → the MIME type the API accepts. */
export const SUPPORTED_UPLOAD_TYPES: Readonly<Record<string, string>> = Object.fromEntries(
  UPLOAD_FORMATS.flatMap((format) => format.extensions.map((ext) => [ext, format.mimeType])),
);

/** For a file input's `accept`: the extensions, plus the PDF MIME type (pickers on some systems match by type). */
export const UPLOAD_ACCEPT = [...Object.keys(SUPPORTED_UPLOAD_TYPES), "application/pdf"].join(",");

/** The extensions as a sentence fragment for hints and refusals: ".md, .txt, .html, .rst, .pdf, .yaml, .yml or .json". */
export const UPLOAD_EXTENSIONS_TEXT = (() => {
  const shown = [".md", ".txt", ".html", ".rst", ".pdf", ".yaml", ".yml", ".json"];
  return `${shown.slice(0, -1).join(", ")} or ${shown[shown.length - 1]}`;
})();

/** The upload cap's default (EAP_KB_MAX_DOCUMENT_BYTES, 5 MiB); the API is the authority. */
export const DEFAULT_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024;
/** EAP_KB_MAX_STRUCTURED_BYTES' default: YAML and JSON (2 MiB). */
export const DEFAULT_MAX_STRUCTURED_BYTES = 2 * 1024 * 1024;
/** EAP_KB_MAX_BINARY_BYTES' default: PDF, decoded (25 MiB). */
export const DEFAULT_MAX_BINARY_BYTES = 25 * 1024 * 1024;
/** The request guard's allowance over the base64 body (eap_knowledge.config.UPLOAD_ENVELOPE_BYTES). */
export const UPLOAD_ENVELOPE_BYTES = 64 * 1024;

/** The per-class caps, in bytes of the file (decoded). */
export interface UploadCaps {
  text: number;
  structured: number;
  binary: number;
}

export const DEFAULT_UPLOAD_CAPS: Readonly<UploadCaps> = {
  text: DEFAULT_MAX_DOCUMENT_BYTES,
  structured: DEFAULT_MAX_STRUCTURED_BYTES,
  binary: DEFAULT_MAX_BINARY_BYTES,
};

/** A format's cap (ExtractLimits.max_bytes_for): structured is the smaller of its cap and the text cap. */
export function capFor(format: Pick<UploadFormat, "capClass">, caps: UploadCaps = DEFAULT_UPLOAD_CAPS): number {
  if (format.capClass === "structured") return Math.min(caps.text, caps.structured);
  return format.capClass === "binary" ? caps.binary : caps.text;
}

/** The upload route's request-body guard: `max(text cap, ceil(binary cap × 4/3) + 64 KiB)`. */
export function uploadGuardBytes(caps: UploadCaps = DEFAULT_UPLOAD_CAPS): number {
  return Math.max(caps.text, Math.ceil((caps.binary * 4) / 3) + UPLOAD_ENVELOPE_BYTES);
}

/** The upload format of `fileName` by its (case-insensitive) extension, or null. */
export function uploadFormatForFile(fileName: string): UploadFormat | null {
  const dot = fileName.lastIndexOf(".");
  if (dot < 0) return null;
  const ext = fileName.slice(dot).toLowerCase();
  return UPLOAD_FORMATS.find((format) => format.extensions.includes(ext)) ?? null;
}

/** The upload format of a MIME type or alias (parameters and case ignored), or null. */
export function uploadFormatForMime(mimeType: string | null | undefined): UploadFormat | null {
  if (!mimeType) return null;
  const base = mimeType.split(";", 1)[0].trim().toLowerCase();
  return UPLOAD_FORMATS.find((format) => format.mimeType === base || format.aliases.includes(base)) ?? null;
}

/** The MIME type for `fileName` by its extension, or null when the format is not supported. */
export function mimeTypeForFile(fileName: string): string | null {
  return uploadFormatForFile(fileName)?.mimeType ?? null;
}

/** "PDF", "YAML", …: a MIME type's readable name (the type itself when it is not in the registry). */
export function formatLabel(mimeType: string): string {
  return uploadFormatForMime(mimeType)?.label ?? mimeType;
}

const MAX_KEY_LENGTH = 256;

/**
 * A valid document key from a file name (or a pasted title): `/` and `\` and control characters
 * become `-`, surrounding whitespace goes, at most 256 characters, never only dots. Empty → null.
 */
export function documentKeyFrom(name: string): string | null {
  // eslint-disable-next-line no-control-regex
  let key = name.replace(/[/\\\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, "-").trim();
  if (key.length > MAX_KEY_LENGTH) key = key.slice(0, MAX_KEY_LENGTH).trim();
  if (!key || /^\.+$/.test(key)) return null;
  return key;
}

/** Why `key` is not a valid document key (the API's rule), or null. */
export function documentKeyProblem(key: string): string | null {
  if (key.length < 1 || key.length > MAX_KEY_LENGTH) return `The key must be 1 to ${MAX_KEY_LENGTH} characters.`;
  // eslint-disable-next-line no-control-regex
  if (/[/\\\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(key) || key !== key.trim()) return "The key must not contain / or \\, control characters, or surrounding spaces.";
  if (/^\.+$/.test(key)) return "The key must not be only dots.";
  return null;
}

/** A title from a file name: the name without its extension. */
export function titleFromFileName(fileName: string): string {
  const dot = fileName.lastIndexOf(".");
  const base = dot > 0 ? fileName.slice(0, dot) : fileName;
  return base.trim() || fileName;
}

/** Thrown by `readFileAsText` for bytes that are not valid UTF-8. */
export class NotUtf8Error extends Error {
  constructor() {
    super("The file is not valid UTF-8 text. Save it as UTF-8 and upload it again.");
    this.name = "NotUtf8Error";
  }
}

/** Decodes `bytes` as UTF-8, strictly: invalid sequences throw NotUtf8Error instead of becoming U+FFFD. */
export function decodeUtf8(bytes: ArrayBuffer | Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new NotUtf8Error();
  }
}

function readFileBytes(file: Blob): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) resolve(reader.result);
      else reject(new Error("The file could not be read"));
    };
    reader.onerror = () => reject(reader.error ?? new Error("The file could not be read"));
    reader.readAsArrayBuffer(file);
  });
}

/**
 * The MIME type of a known binary signature at offset 0 of `bytes`, or null: the mirror of the API's
 * `eap_knowledge.formats.detect_binary_type` (PDF, PNG, JPEG, WebP). Offset 0 only, so text that
 * merely mentions "%PDF-1.7" is never misread.
 */
export function detectBinaryType(bytes: ArrayBuffer | Uint8Array): string | null {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const startsWith = (signature: readonly number[], offset = 0) => signature.every((byte, i) => view[offset + i] === byte);
  if (startsWith([0x25, 0x50, 0x44, 0x46, 0x2d])) return "application/pdf"; // %PDF-
  if (startsWith([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return "image/png";
  if (startsWith([0xff, 0xd8, 0xff])) return "image/jpeg";
  if (view.length >= 12 && startsWith([0x52, 0x49, 0x46, 0x46]) && startsWith([0x57, 0x45, 0x42, 0x50], 8)) return "image/webp"; // RIFF....WEBP
  return null;
}

/** The API's PDF_SIGNATURE_WINDOW (eap_knowledge.formats): `%PDF-` must start in the first 1 KiB of a PDF upload. */
export const PDF_SIGNATURE_WINDOW = 1024;

/**
 * The mirror of the API's `has_pdf_signature`: `%PDF-` starts before byte PDF_SIGNATURE_WINDOW (it
 * may end past it; junk before the header is legal). Only for files uploaded AS PDF: a text
 * upload's check is `detectBinaryType` at offset 0, as on the server, so prose that mentions
 * "%PDF-" is never refused.
 */
export function hasPdfSignature(bytes: ArrayBuffer | Uint8Array): boolean {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const signature = [0x25, 0x50, 0x44, 0x46, 0x2d];
  const last = Math.min(PDF_SIGNATURE_WINDOW - 1, view.length - signature.length);
  for (let start = 0; start <= last; start += 1) {
    if (signature.every((byte, i) => view[start + i] === byte)) return true;
  }
  return false;
}

const UNSUPPORTED_CONTENT = "The file's content does not match its declared type.";

/**
 * Thrown when a file's bytes are not its declared type, with the API's 415 `unsupported_content`
 * message: a text upload that is really a binary file ("It looks like application/pdf, not
 * text/plain."), or a PDF upload with no `%PDF-` in its first 1 KiB.
 */
export class ContentMismatchError extends Error {
  readonly detected: string | null;
  readonly declared: string;
  constructor(detected: string | null, declared: string, reason?: string) {
    super(detected ? `${UNSUPPORTED_CONTENT} It looks like ${detected}, not ${declared}.` : `${UNSUPPORTED_CONTENT} ${reason ?? ""}`.trim());
    this.name = "ContentMismatchError";
    this.detected = detected;
    this.declared = declared;
  }
}

/** Refuses a PDF upload whose first bytes have no `%PDF-` in the window (the API's 415, same message). */
async function checkPdfSignature(file: Blob, declaredType: string): Promise<void> {
  const head = await readFileBytes(file.slice(0, PDF_SIGNATURE_WINDOW + 4));
  if (hasPdfSignature(head)) return;
  throw new ContentMismatchError(
    detectBinaryType(head),
    declaredType,
    `It has no %PDF- signature in its first ${PDF_SIGNATURE_WINDOW.toLocaleString("en-US")} bytes, so it is not a PDF.`,
  );
}

/**
 * Reads `file` (FileReader) and decodes it as strict UTF-8 (see decodeUtf8). With `declaredType`
 * (the MIME type it will be uploaded as), a file that starts with a binary signature is refused
 * first with ContentMismatchError, which names what it looks like, instead of "not valid UTF-8".
 */
export async function readFileAsText(file: Blob, declaredType?: string): Promise<string> {
  const bytes = await readFileBytes(file);
  if (declaredType) {
    const detected = detectBinaryType(bytes);
    if (detected) throw new ContentMismatchError(detected, declaredType);
  }
  return decodeUtf8(bytes);
}

/** A file the browser cannot send as its declared type (not UTF-8, or a binary file under a text
 * name): retrying cannot change the outcome, unlike a failed request. */
export function isUnreadableFileError(reason: unknown): reason is NotUtf8Error | ContentMismatchError {
  return reason instanceof NotUtf8Error || reason instanceof ContentMismatchError;
}

/**
 * Standard base64 (with padding) of `bytes`, in slices so a large buffer never overflows the call
 * stack. Synchronous: uploads use `readFileAsBase64` (FileReader, off the main thread) instead.
 */
export function bytesToBase64(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const SLICE = 0x8000;
  let binary = "";
  for (let i = 0; i < view.length; i += SLICE) binary += String.fromCharCode(...view.subarray(i, i + SLICE));
  return btoa(binary);
}

/**
 * Reads `file` as base64, for `content_base64` (binary formats such as PDF): FileReader's
 * `readAsDataURL` encodes natively and asynchronously (no main-thread loop over the bytes, no
 * intermediate binary string), and the `data:…;base64,` prefix is cut off.
 */
export function readFileAsBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = typeof reader.result === "string" ? reader.result : "";
      const comma = url.indexOf(",");
      if (comma < 0 || !url.slice(0, comma).endsWith(";base64")) {
        reject(new Error("The file could not be read"));
        return;
      }
      resolve(url.slice(comma + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error("The file could not be read"));
    reader.readAsDataURL(file);
  });
}

/** How many bytes standard base64 `value` decodes to (padding counted). Arithmetic only: no copy of
 * the string (FileReader's base64 has no whitespace). */
export function base64DecodedLength(value: string): number {
  const padding = value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor((value.length * 3) / 4) - padding);
}

/** The upload body for a file of `format`: `content_base64` for a binary format, else UTF-8 `content`.
 * With `format.mimeType`, refused with ContentMismatchError as the API would: a text file whose
 * bytes start with a binary signature, or a PDF with no `%PDF-` in its first 1 KiB. */
export async function readUploadContent(
  file: Blob,
  format: Pick<UploadFormat, "binary"> & { mimeType?: string },
): Promise<Pick<DocumentPut, "content" | "content_base64">> {
  if (!format.binary) return { content: await readFileAsText(file, format.mimeType) };
  if (format.mimeType && uploadFormatForMime(format.mimeType)?.format === "pdf") await checkPdfSignature(file, format.mimeType);
  return { content_base64: await readFileAsBase64(file) };
}

/** "Text up to 5 MiB, YAML and JSON up to 2 MiB, PDF up to 25 MiB." (the upload Sheet's hint). */
export function uploadCapsText(caps: UploadCaps = DEFAULT_UPLOAD_CAPS): string {
  const structured = capFor({ capClass: "structured" }, caps);
  return `Text up to ${mibLabel(caps.text)} MiB, YAML and JSON up to ${mibLabel(structured)} MiB, PDF up to ${mibLabel(caps.binary)} MiB.`;
}

/** Why a file is refused before it is read: its chip in the upload list says which. */
export type UploadRefusal = "unsupported_type" | "empty" | "too_large" | "bad_key";

export type UploadCheck =
  | { ok: true; mimeType: string; key: string; title: string; binary: boolean; maxBytes: number }
  | { ok: false; reason: string; code: UploadRefusal };

function mib(bytes: number): number {
  return Math.round(bytes / (1024 * 1024));
}

/** "5", "2", "25", or "0.5" below 1 MiB. */
function mibLabel(bytes: number): string {
  const value = bytes / (1024 * 1024);
  return value >= 1 ? String(mib(bytes)) : String(Math.round(value * 10) / 10);
}

/**
 * Whether a picked or dropped file can be uploaded, and as what, with its format's cap (text 5 MiB,
 * YAML and JSON 2 MiB, PDF 25 MiB by default). `maxBytes` overrides the text cap (EAP_KB_MAX_DOCUMENT_BYTES),
 * which also bounds YAML and JSON. The size test here is only a pre-check; the real check is
 * `documentPutProblem` on the encoded body, made once the file is read, and the API's own.
 */
export function checkUploadFile(file: { name: string; size: number }, maxBytes: number = DEFAULT_MAX_DOCUMENT_BYTES, caps: UploadCaps = DEFAULT_UPLOAD_CAPS): UploadCheck {
  const format = uploadFormatForFile(file.name);
  if (!format) return { ok: false, code: "unsupported_type", reason: `Unsupported file type: upload ${UPLOAD_EXTENSIONS_TEXT} files.` };
  if (file.size === 0) return { ok: false, code: "empty", reason: "The file is empty." };
  const cap = capFor(format, { ...caps, text: maxBytes });
  if (file.size > cap) return { ok: false, code: "too_large", reason: `The file is larger than ${mibLabel(cap)} MiB.` };
  const key = documentKeyFrom(file.name);
  if (!key) return { ok: false, code: "bad_key", reason: "The file name cannot be used as a document key." };
  return { ok: true, mimeType: format.mimeType, key, title: titleFromFileName(file.name), binary: format.binary, maxBytes: cap };
}

/**
 * Metadata typed into the upload form, one `key: value` (or `key=value`) per line. Numbers and
 * true/false become numbers and booleans; quotes keep a value a string. At most 16 keys, each
 * `[A-Za-z0-9_.-]{1,64}` (the API's rule).
 */
export function parseMetadata(text: string): { ok: true; value: Record<string, MetadataValue> | null } | { ok: false; reason: string } {
  const out: Record<string, MetadataValue> = {};
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  for (const line of lines) {
    const match = /^([^:=]+?)\s*[:=]\s*(.*)$/.exec(line);
    if (!match) return { ok: false, reason: `"${line}" is not key: value.` };
    const key = match[1].trim();
    if (!/^[A-Za-z0-9_.-]{1,64}$/.test(key)) return { ok: false, reason: `"${key}" is not a valid key (letters, digits, _ . -; up to 64).` };
    out[key] = scalar(match[2].trim());
  }
  const keys = Object.keys(out);
  if (keys.length > 16) return { ok: false, reason: "At most 16 metadata keys." };
  return { ok: true, value: keys.length ? out : null };
}

function scalar(raw: string): MetadataValue {
  if (/^".*"$|^'.*'$/.test(raw)) return raw.slice(1, -1);
  if (raw === "true") return true;
  if (raw === "false") return false;
  if (/^-?\d+(\.\d+)?$/.test(raw) && Number.isFinite(Number(raw))) return Number(raw);
  return raw;
}

/**
 * A search filter typed as JSON (`{"lang": "en", "tier": ["a", "b"]}`): an object of scalars or
 * lists of scalars. Empty → null.
 */
export function parseFilter(text: string): { ok: true; value: Record<string, MetadataValue | MetadataValue[]> | null } | { ok: false; reason: string } {
  if (!text.trim()) return { ok: true, value: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false, reason: "The filter is not valid JSON." };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return { ok: false, reason: "The filter must be a JSON object." };
  const isScalar = (v: unknown) => typeof v === "string" || typeof v === "number" || typeof v === "boolean";
  for (const [key, value] of Object.entries(parsed)) {
    if (!(isScalar(value) || (Array.isArray(value) && value.every(isScalar)))) {
      return { ok: false, reason: `"${key}" must be a string, number, boolean or a list of them.` };
    }
  }
  return { ok: true, value: Object.keys(parsed).length ? (parsed as Record<string, MetadataValue | MetadataValue[]>) : null };
}

// ---------------------------------------------------------------------------------------------
// Ingest progress and errors

export interface IngestProgress {
  embedded: number;
  total: number;
}

/** The latest `ingest_progress` of a kb_ingest run's events, or null before the first one. */
export function ingestProgress(events: readonly Pick<RunEvent, "type" | "data">[]): IngestProgress | null {
  let latest: IngestProgress | null = null;
  for (const event of events) {
    if ((event.type as string) !== "ingest_progress") continue;
    const { embedded, total } = event.data as { embedded?: unknown; total?: unknown };
    if (typeof embedded === "number" && typeof total === "number") latest = { embedded, total };
  }
  return latest;
}

/** "3 of 12 sections embedded". */
export function progressLabel(progress: IngestProgress): string {
  return `${progress.embedded} of ${progress.total} section${progress.total === 1 ? "" : "s"} embedded`;
}

const INGEST_ERROR_MESSAGES: Record<string, string> = {
  embedding_failed: "The embedding provider failed.",
  embedding_model_not_allowed: "The embedding model is no longer allowed.",
  embedding_dimension_mismatch: "The embedding model returned vectors of the wrong size.",
  ingest_budget_exceeded: "The ingest would embed more tokens than allowed per run (EAP_KB_MAX_INGEST_TOKENS).",
  extraction_failed: "No text could be extracted from the document.",
  knowledge_base_limit_exceeded: "The knowledge base is at its document or section limit.",
  ingest_cancelled: "The ingest was cancelled.",
  ingest_not_converged: "The ingest kept racing a reindex and gave up; retry it.",
  internal_error: "The ingest failed with an internal error.",
};

/**
 * The extraction failure reasons (eap_schemas.knowledge.EXTRACTION_ERROR_MESSAGES): a document's
 * `error_reason`, whose fixed message is the rest of its `extraction_failed: …` error.
 */
export const EXTRACTION_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  extract_failed: "The document could not be extracted.",
  extract_timeout: "Extraction took too long and was stopped.",
  extract_memory: "Extraction ran out of memory.",
  extract_limit: "The document is over an extraction limit (pages, nodes, nesting or image size).",
  extract_output_too_large: "The extracted text is too large.",
  document_encrypted: "The PDF is encrypted with a password.",
  no_extractable_text: "No text could be extracted from the document.",
  unsupported_content: "The file's content does not match its declared type.",
  image_model_required: "Images need an image-capable embedding model.",
};

/** A document's or knowledge base's `error` ("<code>: detail") as readable text. */
export function describeIngestError(error: string | null | undefined): string {
  if (!error) return "The ingest failed.";
  const match = /^([a-z_]+)(?::\s*([\s\S]*))?$/.exec(error.trim());
  if (match && INGEST_ERROR_MESSAGES[match[1]]) {
    const detail = match[2]?.trim();
    // An extraction failure's detail is the reason's fixed message: shown on its own.
    if (match[1] === "extraction_failed" && detail && Object.values(EXTRACTION_ERROR_MESSAGES).includes(detail)) return detail;
    return detail ? `${INGEST_ERROR_MESSAGES[match[1]]} (${detail})` : INGEST_ERROR_MESSAGES[match[1]];
  }
  return error;
}

/** A document's failure as readable text: its `error_reason`'s fixed message when the API named one, else `describeIngestError`. */
export function describeDocumentError(document: Pick<KnowledgeDocument, "error" | "error_reason">): string {
  const reason = document.error_reason ? EXTRACTION_ERROR_MESSAGES[document.error_reason] : undefined;
  return reason ?? describeIngestError(document.error);
}

// ---------------------------------------------------------------------------------------------
// Extraction (RAG P1)

/** The extractor ids' names (EXTRACTOR_VERSIONS keys). */
export const EXTRACTOR_LABELS: Readonly<Record<string, string>> = {
  rst: "reStructuredText",
  pdf: "PDF",
  yaml: "YAML",
  json: "JSON",
  "markdown-doxygen": "Doxygen Markdown",
};

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

/** True when the document was extracted by an older version of its extractor than the current one. */
export function extractionOutdated(extraction: Pick<DocumentExtraction, "extractor_version" | "current_version"> | null | undefined): boolean {
  return !!extraction && typeof extraction.current_version === "number" && extraction.extractor_version < extraction.current_version;
}

/** "PDF extractor v1 · 212 pages · 340 sections · 3 warnings" for the document Sheet. */
export function extractionSummary(extraction: DocumentExtraction): string {
  const parts = [`${EXTRACTOR_LABELS[extraction.extractor] ?? extraction.extractor} extractor v${extraction.extractor_version}`];
  if (typeof extraction.pages === "number") parts.push(plural(extraction.pages, "page"));
  if (typeof extraction.sections === "number") parts.push(plural(extraction.sections, "section"));
  if (typeof extraction.conditions === "number" && extraction.conditions > 0) parts.push(plural(extraction.conditions, "condition"));
  if (typeof extraction.warnings === "number" && extraction.warnings > 0) parts.push(plural(extraction.warnings, "warning"));
  return parts.join(" · ");
}

/** "3 pages have no text: scanned?" for a PDF's image-only pages, or null when there are none. */
export function imageOnlyPagesText(pages: readonly number[] | null | undefined): string | null {
  if (!pages || pages.length === 0) return null;
  return pages.length === 1 ? "1 page has no text: scanned?" : `${pages.length.toLocaleString("en-US")} pages have no text: scanned?`;
}

/** "p. 3, 7, 12" (at most `max` page numbers, then "…"). */
export function pageListText(pages: readonly number[], max = 12): string {
  const shown = pages.slice(0, max).join(", ");
  return `${pages.length === 1 ? "p." : "pp."} ${shown}${pages.length > max ? ", …" : ""}`;
}

/** The knowledge base's chunker version (`chunking.chunker_version`; 1 when it is not recorded). */
export function chunkerVersionOf(kb: Pick<KnowledgeBase, "chunking"> | null | undefined): number {
  const value = (kb?.chunking as { chunker_version?: unknown } | undefined)?.chunker_version;
  return typeof value === "number" ? value : 1;
}

/** The "Advanced extraction" fields of the create dialog, as typed. */
export interface ExtractForm {
  /** "" leaves it unset (Markdown stays on the API path). */
  markdownFlavor: "" | MarkdownFlavor;
  /** One `NAME: value` per line (ifconfig names). */
  rstConfig: string;
  /** Tags for `only`, separated by commas, spaces or new lines. */
  rstTags: string;
  /** "" leaves it unset (outline, falling back to font). */
  pdfHeadings: "" | PdfHeadings;
}

export const EMPTY_EXTRACT_FORM: Readonly<ExtractForm> = { markdownFlavor: "", rstConfig: "", rstTags: "", pdfHeadings: "" };

const RST_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const MAX_RST_ENTRIES = 64;

/**
 * `chunking.extract` from the form: only the fields that are set (an empty form → null, so the
 * request carries no `extract` at all). Names follow the API's rule (`[A-Za-z_][A-Za-z0-9_]{0,63}`,
 * at most 64 config keys and 64 tags).
 */
export function buildExtractConfig(form: ExtractForm): { ok: true; value: ExtractConfig | null } | { ok: false; reason: string } {
  const out: ExtractConfig = {};
  if (form.markdownFlavor) out.markdown_flavor = form.markdownFlavor;
  if (form.pdfHeadings) out.pdf_headings = form.pdfHeadings;
  const config: Record<string, MetadataValue> = {};
  for (const line of form.rstConfig.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)) {
    const match = /^([^:=]+?)\s*[:=]\s*(.*)$/.exec(line);
    if (!match) return { ok: false, reason: `rst config: "${line}" is not NAME: value.` };
    const name = match[1].trim();
    if (!RST_NAME.test(name)) return { ok: false, reason: `rst config: "${name}" is not a valid name (a letter or _, then letters, digits or _; up to 64).` };
    config[name] = scalar(match[2].trim());
  }
  const configKeys = Object.keys(config);
  if (configKeys.length > MAX_RST_ENTRIES) return { ok: false, reason: `rst config: at most ${MAX_RST_ENTRIES} names.` };
  if (configKeys.length) out.rst_config = config;
  const tags = form.rstTags.split(/[\s,]+/).filter(Boolean);
  for (const tag of tags) {
    if (!RST_NAME.test(tag)) return { ok: false, reason: `rst tags: "${tag}" is not a valid tag (a letter or _, then letters, digits or _; up to 64).` };
  }
  const unique = Array.from(new Set(tags));
  if (unique.length > MAX_RST_ENTRIES) return { ok: false, reason: `rst tags: at most ${MAX_RST_ENTRIES} tags.` };
  if (unique.length) out.rst_tags = unique;
  return { ok: true, value: Object.keys(out).length ? out : null };
}

export const DOCUMENT_STATUS_LABELS: Record<DocumentStatus, string> = {
  pending: "Pending",
  ingesting: "Ingesting",
  ready: "Ready",
  failed: "Failed",
};

export const KNOWLEDGE_BASE_STATUS_LABELS: Record<KnowledgeBaseStatus, string> = {
  ready: "Ready",
  reindexing: "Reindexing",
  reindex_failed: "Reindex failed",
};

/** A document whose ingest has not ended: its run is worth following. */
export function isIngestInFlight(document: Pick<KnowledgeDocument, "status">): boolean {
  return document.status === "pending" || document.status === "ingesting";
}

/**
 * The status to show: "ingesting" for a pending document whose run's live stream already reported
 * `run_started` or `ingest_progress` (the 3 s re-read has not caught up yet); else the stored one.
 */
export function displayedDocumentStatus(
  document: Pick<KnowledgeDocument, "status" | "last_run_id">,
  startedRuns: ReadonlySet<string> | null | undefined,
): DocumentStatus {
  if (document.status === "pending" && document.last_run_id && startedRuns?.has(document.last_run_id)) return "ingesting";
  return document.status;
}

/**
 * How long a document may stay `pending` with no usable run before Studio offers Retry: the API's
 * ORPHAN_AFTER_SECONDS (eap_api.routes.knowledge), after which POST …/reingest hands the run off
 * again (a stale `queued` run) or starts a new one (no run recorded).
 */
export const STUCK_PENDING_AFTER_MS = 60_000;

/** What `ingestStuck` needs of the document's `last_run_id` run: null when there is none (no
 * `last_run_id`, or GET /runs/{id} answered 404); undefined when it is not known yet. */
export type IngestRunState = { status: string; created_at: string } | null | undefined;

const RUN_TERMINAL = new Set(["completed", "failed", "needs_attention"]);

function olderThan(iso: string, ms: number, now: number): boolean {
  const at = Date.parse(iso);
  return Number.isFinite(at) && now - at > ms;
}

/**
 * Whether a `pending` document's ingest never started, so Retry (reingest) is the way out. The
 * document stays `pending` when its run's hand-off failed: the run is `failed` (error
 * "EnqueueFailed…") or otherwise ended while the document still waits, or it sat `queued` past
 * STUCK_PENDING_AFTER_MS (never enqueued). With no run at all, the document itself must be that
 * old (a younger one belongs to a request still starting it). The reingest route accepts each case.
 */
export function ingestStuck(
  document: Pick<KnowledgeDocument, "status" | "updated_at">,
  run: IngestRunState,
  now: number = Date.now(),
): boolean {
  return ingestStuckReason(document, run, now) !== null;
}

/** Why a pending document is stuck (see `ingestStuck`): `not_started` (its run ended, or there is
 * none), `queued` (its run has waited past STUCK_PENDING_AFTER_MS), or null when it is not. */
export type IngestStuckReason = "not_started" | "queued";

export function ingestStuckReason(
  document: Pick<KnowledgeDocument, "status" | "updated_at">,
  run: IngestRunState,
  now: number = Date.now(),
): IngestStuckReason | null {
  if (document.status !== "pending" || run === undefined) return null;
  if (run === null) return olderThan(document.updated_at, STUCK_PENDING_AFTER_MS, now) ? "not_started" : null;
  if (RUN_TERMINAL.has(run.status)) return "not_started";
  return run.status === "queued" && olderThan(run.created_at, STUCK_PENDING_AFTER_MS, now) ? "queued" : null;
}

/** The Documents tab's line for a stuck document. */
export const STUCK_INGEST_TEXT: Record<IngestStuckReason, string> = {
  not_started: "The ingest did not start. Retry starts it again.",
  queued: "Still queued after a minute. Retry re-queues it.",
};

// ---------------------------------------------------------------------------------------------
// Links and references

/** True for an absolute http(s) URL: the only source URLs Studio renders as links. */
export { isSafeHttpUrl } from "./safeUrl";

/** A knowledge base reference in an agent spec (`AgentSpec.knowledge_bases`, RM.2). */
export interface KnowledgeBaseRef {
  id: string;
  top_k?: number;
  min_score?: number | null;
  filter?: Record<string, MetadataValue | MetadataValue[]> | null;
}

/** The knowledge base ids a spec attaches (empty for specs without `knowledge_bases`). */
export function knowledgeBaseIdsOf(spec: AgentSpec | null | undefined): string[] {
  const refs = (spec as { knowledge_bases?: unknown } | null | undefined)?.knowledge_bases;
  if (!Array.isArray(refs)) return [];
  return refs.flatMap((ref) => (ref && typeof ref === "object" && typeof (ref as { id?: unknown }).id === "string" ? [(ref as { id: string }).id] : []));
}

/** "provider:name (1024 dims)". */
export function embeddingModelLabel(model: Pick<EmbeddingModel, "id" | "dimensions">): string {
  return `${model.id} (${model.dimensions} dims)`;
}
