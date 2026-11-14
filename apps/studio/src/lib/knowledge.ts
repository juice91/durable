// Knowledge base client (/api/v1/knowledge-bases, /api/v1/embedding-models, and the knowledge base's
// runs on /api/v1/runs) and the pure helpers the Knowledge pages use (RM.16).
//
// Uploads are JSON: Studio reads each file as UTF-8 in the browser and maps its extension to the MIME
// type (decision "Document formats and upload"); PDF and other binary formats are refused here.
import { apiErrorFromResponse, apiHeaders, ApiError, BASE_URL, type AgentSpec } from "./api";
import type { Run, RunEvent } from "./runs";

// ---------------------------------------------------------------------------------------------
// Shapes (apps/api/src/eap_api/schemas.py)

export type KnowledgeBaseStatus = "ready" | "reindexing" | "reindex_failed";

export interface ChunkingConfig {
  max_tokens: number;
  overlap_tokens: number;
  min_tokens: number;
  chunker_version?: number;
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
  chunking?: ChunkingConfig | null;
}

export interface DocumentPut {
  title?: string | null;
  content: string;
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

export interface KnowledgeBaseReference {
  agent_id: string;
  agent_name: string;
  version: number;
  archived: boolean;
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

/** The exact JSON body `putDocument` sends (the API caps the request body, not the content). */
export function documentPutJson(key: string, body: DocumentPut): string {
  return JSON.stringify({ document_key: key, ...body });
}

/** The UTF-8 size of that body, which EAP_KB_MAX_DOCUMENT_BYTES limits. */
export function documentPutBytes(key: string, body: DocumentPut): number {
  return new TextEncoder().encode(documentPutJson(key, body)).length;
}

/** Why the upload body is over the cap (as the API would answer 413), or null. */
export function documentPutProblem(key: string, body: DocumentPut, maxBytes: number = DEFAULT_MAX_DOCUMENT_BYTES): string | null {
  return documentPutBytes(key, body) > maxBytes ? `The upload is larger than ${mib(maxBytes)} MiB once encoded (JSON).` : null;
}

/** Upload (or replace) a document. 200 unchanged; 202 stored with an ingest run (`run_id`). */
export function putDocument(kbId: string, key: string, body: DocumentPut, init: RequestInit = {}): Promise<DocumentPutResult> {
  return request<DocumentPutResult>(docPath(kbId, key), { ...init, method: "PUT", body: documentPutJson(key, body) });
}

export function deleteDocument(kbId: string, key: string): Promise<void> {
  return request<void>(docPath(kbId, key), { method: "DELETE" });
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

/** Extension (lower case, with the dot) → the MIME type the API accepts. */
export const SUPPORTED_UPLOAD_TYPES: Readonly<Record<string, string>> = {
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".txt": "text/plain",
  ".text": "text/plain",
  ".html": "text/html",
  ".htm": "text/html",
};

/** For a file input's `accept`. */
export const UPLOAD_ACCEPT = Object.keys(SUPPORTED_UPLOAD_TYPES).join(",");

/** The upload cap's default (EAP_KB_MAX_DOCUMENT_BYTES, 5 MiB); the API is the authority. */
export const DEFAULT_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024;

/** The MIME type for `fileName` by its extension, or null when the format is not supported. */
export function mimeTypeForFile(fileName: string): string | null {
  const dot = fileName.lastIndexOf(".");
  if (dot < 0) return null;
  return SUPPORTED_UPLOAD_TYPES[fileName.slice(dot).toLowerCase()] ?? null;
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

/** Reads `file` as UTF-8 text (FileReader). */
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

/** Reads `file` (FileReader) and decodes it as strict UTF-8 (see decodeUtf8). */
export function readFileAsText(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      try {
        resolve(reader.result instanceof ArrayBuffer ? decodeUtf8(reader.result) : String(reader.result ?? ""));
      } catch (error) {
        reject(error);
      }
    };
    reader.onerror = () => reject(reader.error ?? new Error("The file could not be read"));
    reader.readAsArrayBuffer(file);
  });
}

export type UploadCheck = { ok: true; mimeType: string; key: string; title: string } | { ok: false; reason: string };

function mib(bytes: number): number {
  return Math.round(bytes / (1024 * 1024));
}

/**
 * Whether a picked or dropped file can be uploaded, and as what. The size test here is only a
 * pre-check (content alone over the cap can never fit); the real check is `documentPutProblem` on
 * the encoded body, made once the file is read.
 */
export function checkUploadFile(file: { name: string; size: number }, maxBytes: number = DEFAULT_MAX_DOCUMENT_BYTES): UploadCheck {
  const mimeType = mimeTypeForFile(file.name);
  if (!mimeType) return { ok: false, reason: "Unsupported file type: upload .md, .txt or .html files." };
  if (file.size === 0) return { ok: false, reason: "The file is empty." };
  if (file.size > maxBytes) return { ok: false, reason: `The file is larger than ${mib(maxBytes)} MiB.` };
  const key = documentKeyFrom(file.name);
  if (!key) return { ok: false, reason: "The file name cannot be used as a document key." };
  return { ok: true, mimeType, key, title: titleFromFileName(file.name) };
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

/** A document's or knowledge base's `error` ("<code>: detail") as readable text. */
export function describeIngestError(error: string | null | undefined): string {
  if (!error) return "The ingest failed.";
  const match = /^([a-z_]+)(?::\s*([\s\S]*))?$/.exec(error.trim());
  if (match && INGEST_ERROR_MESSAGES[match[1]]) {
    const detail = match[2]?.trim();
    return detail ? `${INGEST_ERROR_MESSAGES[match[1]]} (${detail})` : INGEST_ERROR_MESSAGES[match[1]];
  }
  return error;
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
