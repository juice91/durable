// Knowledge-base sources and their connections (/api/v1/settings/connections,
// /api/v1/knowledge-bases/{kb}/sources) and the published "KB Source Synchronization" workflow
// Studio starts syncs through. A connection holds `secret:<name>` references only: no value is ever
// typed into, returned by or shown through this module.
import { apiHeaders, BASE_URL } from "./api";
import { listKnowledgeBases } from "./knowledge";
import { secretRef } from "./secrets";
import { listWorkflows, startWorkflowRun, type Workflow } from "./workflows";

export const SYNC_WORKFLOW_NAME = "KB Source Synchronization";
export const CONNECTION_KIND = "oauth2_client_credentials";
export const SOURCE_KIND = "product_catalog";
/** Providers an adapter exists for. */
export const PROVIDERS = [{ value: "acme_catalog", label: "Acme Semiconductor", baseUrl: "https://api.vendor.example", tokenUrl: "https://api.vendor.example/v1/oauth/accesstoken" }] as const;
export const MAX_TARGETED_PARTS = 100;

export type SyncMode = "targeted" | "incremental" | "full";
export const SYNC_MODES: readonly SyncMode[] = ["targeted", "incremental", "full"];

/** An API error with the response's stable `code` (the body is never echoed beyond it). */
export class KbSourceError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
    this.name = "KbSourceError";
  }
}

const CODE_TEXT: Record<string, string> = {
  connection_name_conflict: "A connection with this name already exists.",
  source_name_conflict: "This knowledge base already has a source with this name.",
  connection_in_use: "A source still uses this connection.",
  connection_not_found: "The connection no longer exists.",
  knowledge_base_not_found: "The knowledge base no longer exists.",
  source_not_found: "The source no longer exists.",
  source_adapter_not_found: "There is no adapter for this provider.",
  secret_ref_required: "Credentials must be stored secrets; choose a secret name.",
  sync_workflow_missing: `The ${SYNC_WORKFLOW_NAME} workflow is not published yet.`,
  workflow_template_unavailable: "The sync workflow template is not available on this server.",
};

export const CHECK_CODE_TEXT: Record<string, string> = {
  connection_not_found: "The connection was not found.",
  connection_disabled: "The connection is disabled; enable it first.",
  secret_missing: "A stored secret the connection refers to is missing.",
  connection_auth_failed: "The provider rejected the credentials.",
  connection_unreachable: "The provider could not be reached.",
  connection_response_invalid: "The provider answered something unexpected.",
};

export interface CheckResult {
  ok: boolean;
  checked_at: string;
  error_code: string | null;
}

export function checkText(result: CheckResult): string {
  if (result.ok) return "Connection ok";
  const code = result.error_code ?? "unknown";
  return CHECK_CODE_TEXT[code] ?? `The check failed (${code}).`;
}

async function failure(response: Response): Promise<KbSourceError> {
  let code = "";
  let detail = "";
  try {
    const data = await response.json();
    if (typeof data?.code === "string") code = data.code;
    if (typeof data?.detail === "string") detail = data.detail;
  } catch {
    // Not JSON: the status text below.
  }
  const text = CODE_TEXT[code] ?? (detail && response.status < 500 ? detail.slice(0, 300) : `The request failed (HTTP ${response.status}).`);
  return new KbSourceError(response.status, code, text);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}/api/v1${path}`, {
    ...init,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
  });
  if (!response.ok) throw await failure(response);
  if (response.status === 204) return undefined as T;
  return response.json();
}

const enc = encodeURIComponent;

// ---------------------------------------------------------------------------------------------
// Connections

export interface Connection {
  id: string;
  name: string;
  kind: string;
  base_url: string | null;
  config: Record<string, unknown>;
  /** logical name -> `secret:<name>` */
  credential_refs: Record<string, string>;
  enabled: boolean;
  last_checked_at: string | null;
  last_status: "ok" | "error" | null;
  last_error_code: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConnectionBody {
  name: string;
  kind: string;
  base_url?: string;
  config: Record<string, unknown>;
  credential_refs: Record<string, string>;
  enabled: boolean;
}

export interface ConnectionForm {
  name: string;
  provider: string;
  baseUrl: string;
  tokenUrl: string;
  clientIdSecret: string;
  clientSecretSecret: string;
}

/** The request body for a connection form: secret NAMES become `secret:<name>` references. */
export function connectionPayload(form: ConnectionForm, enabled = true): ConnectionBody {
  const config: Record<string, unknown> = { provider: form.provider };
  if (form.tokenUrl.trim()) config.token_url = form.tokenUrl.trim();
  const body: ConnectionBody = {
    name: form.name.trim(),
    kind: CONNECTION_KIND,
    config,
    credential_refs: { client_id: secretRef(form.clientIdSecret), client_secret: secretRef(form.clientSecretSecret) },
    enabled,
  };
  if (form.baseUrl.trim()) body.base_url = form.baseUrl.trim();
  return body;
}

export async function listConnections(): Promise<Connection[]> {
  return (await request<{ items: Connection[] }>("/settings/connections")).items;
}
export const createConnection = (body: ConnectionBody) => request<Connection>("/settings/connections", { method: "POST", body: JSON.stringify(body) });
export const updateConnection = (id: string, body: Partial<ConnectionBody>) => request<Connection>(`/settings/connections/${enc(id)}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteConnection = (id: string) => request<void>(`/settings/connections/${enc(id)}`, { method: "DELETE" });
export const checkConnection = (id: string) => request<CheckResult>(`/settings/connections/${enc(id)}/check`, { method: "POST" });

// ---------------------------------------------------------------------------------------------
// Sources

export interface Source {
  id: string;
  knowledge_base_id: string;
  connection_id: string;
  kind: string;
  provider: string;
  name: string;
  enabled: boolean;
  config: Record<string, unknown>;
  last_successful_sync_at: string | null;
  last_checkpoint_summary: { segments: number; has_cursor: boolean };
  last_source_revision: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourceCreate {
  connection_id: string;
  name: string;
  kind: string;
  provider: string;
  config: Record<string, unknown>;
  enabled: boolean;
}

export async function listSources(kbId: string): Promise<Source[]> {
  return (await request<{ items: Source[] }>(`/knowledge-bases/${enc(kbId)}/sources`)).items;
}
export const createSource = (kbId: string, body: SourceCreate) => request<Source>(`/knowledge-bases/${enc(kbId)}/sources`, { method: "POST", body: JSON.stringify(body) });
export const updateSource = (kbId: string, id: string, body: { name?: string; config?: Record<string, unknown>; enabled?: boolean }) =>
  request<Source>(`/knowledge-bases/${enc(kbId)}/sources/${enc(id)}`, { method: "PATCH", body: JSON.stringify(body) });

export interface SourceRef {
  id: string;
  name: string;
  knowledgeBaseId: string;
  knowledgeBaseName: string;
}

/** The sources (across knowledge bases) that use a connection: the delete refusal carries no names. */
export async function sourcesUsingConnection(connectionId: string): Promise<SourceRef[]> {
  const bases = await listKnowledgeBases();
  const lists = await Promise.all(bases.map(async (kb) => ({ kb, sources: await listSources(kb.id).catch(() => [] as Source[]) })));
  return lists.flatMap(({ kb, sources }) =>
    sources.filter((s) => s.connection_id === connectionId).map((s) => ({ id: s.id, name: s.name, knowledgeBaseId: kb.id, knowledgeBaseName: kb.name })),
  );
}

// ---------------------------------------------------------------------------------------------
// Syncs

export interface SyncRow {
  run_id: string;
  source_id: string;
  parent_run_id: string | null;
  mode: SyncMode;
  status: string;
  counters: Record<string, unknown>;
  errors: unknown[];
  started_at: string;
  completed_at: string | null;
}

export interface SyncDetail extends SyncRow {
  report: Record<string, unknown> | null;
  run_status: string | null;
}

export async function listSyncs(kbId: string, sourceId: string, limit = 20): Promise<SyncRow[]> {
  return (await request<{ items: SyncRow[] }>(`/knowledge-bases/${enc(kbId)}/sources/${enc(sourceId)}/syncs?limit=${limit}`)).items;
}
export const getSync = (kbId: string, sourceId: string, runId: string) => request<SyncDetail>(`/knowledge-bases/${enc(kbId)}/sources/${enc(sourceId)}/syncs/${enc(runId)}`);

export interface ReportSummary {
  discovered: number;
  inserted: number;
  updated: number;
  unchanged: number;
  documents_queued: number;
  failed: number;
  stop_reason: string | null;
}

const COUNT_KEYS = ["discovered", "inserted", "updated", "unchanged", "documents_queued", "failed"] as const;

/** The bounded report's headline numbers: from the run's report once it ends, else the sync's counters. */
export function reportSummary({ report, counters }: { report?: Record<string, unknown> | null; counters?: Record<string, unknown> | null }): ReportSummary {
  const num = (key: string): number => {
    const value = report?.[key] ?? counters?.[key];
    return typeof value === "number" && Number.isFinite(value) ? value : 0;
  };
  const stop = report?.stop_reason ?? counters?.stop_reason;
  const summary = { stop_reason: typeof stop === "string" && stop ? stop : null } as ReportSummary;
  for (const key of COUNT_KEYS) summary[key] = num(key);
  return summary;
}

/** Part numbers from free text: split on whitespace, commas and semicolons; trimmed, de-duplicated. */
export function parsePartNumbers(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((part) => part.trim()).filter(Boolean))];
}

export interface SyncOptions {
  partNumbers?: string[];
  restart?: boolean;
}

/** The workflow's input: `part_numbers` only for a targeted sync, `restart` only for a full one that asks. */
export function syncInput(sourceId: string, mode: SyncMode, { partNumbers = [], restart = false }: SyncOptions = {}): Record<string, unknown> {
  const input: Record<string, unknown> = { source_id: sourceId, mode };
  if (mode === "targeted") input.part_numbers = partNumbers;
  if (mode === "full" && restart) input.restart = true;
  return input;
}

// ---------------------------------------------------------------------------------------------
// The published sync workflow

/** The tenant's published "KB Source Synchronization" workflow, or null. */
export async function findSyncWorkflow(): Promise<Workflow | null> {
  const all = await listWorkflows();
  return all.find((workflow) => workflow.name === SYNC_WORKFLOW_NAME && workflow.latest_version != null) ?? null;
}

/**
 * Publish the example workflow: reuse an unpublished draft with the name, else create one from the
 * server's template (GET /kb-sources/workflow-template) and publish it.
 */
export async function publishSyncWorkflow(): Promise<Workflow> {
  const all = await listWorkflows();
  const published = all.find((workflow) => workflow.name === SYNC_WORKFLOW_NAME && workflow.latest_version != null);
  if (published) return published;
  let workflow = all.find((item) => item.name === SYNC_WORKFLOW_NAME);
  if (!workflow) {
    const spec = await request<Record<string, unknown>>("/kb-sources/workflow-template");
    workflow = await request<Workflow>("/workflows", { method: "POST", body: JSON.stringify({ spec }) });
  }
  const version = await request<{ version: number }>(`/workflows/${enc(workflow.id)}/versions`, { method: "POST" });
  return { ...workflow, latest_version: version.version };
}

/** Start a sync by running the published workflow with the sync input. */
export async function startSourceSync(sourceId: string, mode: SyncMode, options: SyncOptions = {}): Promise<{ workflowId: string; runId: string }> {
  const workflow = await findSyncWorkflow();
  if (!workflow) throw new KbSourceError(404, "sync_workflow_missing", CODE_TEXT.sync_workflow_missing);
  const accepted = await startWorkflowRun(workflow.id, syncInput(sourceId, mode, options));
  return { workflowId: workflow.id, runId: accepted.run_id };
}
