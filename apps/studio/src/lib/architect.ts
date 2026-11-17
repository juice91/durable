// Architect client (/api/v1/architect/*) and the pure form helpers the Architect page uses.
// Errors from these routes are flat `{detail, code, ...}`; ArchitectError keeps `code` and the rest.
import { apiHeaders, ApiError, BASE_URL } from "./api";

export interface CreatedItem {
  /** The slug. */
  id: string;
  /** The row id (agents list items). */
  agent_id?: string;
  /** The row id (workflow list items). */
  workflow_id?: string;
  name: string;
  versions: number;
  latest_created_via: string | null;
  draft: boolean;
  archived: boolean;
}

export interface ArchitectState {
  seeded: boolean;
  choose_model: boolean;
  architect_agent_id: string | null;
  drafter_agent_id: string | null;
  model: string | null;
  agents: CreatedItem[];
  workflows: CreatedItem[];
}

export interface ArchitectOptions {
  tools: { id: string; title: string; description: string; requires_approval: boolean }[];
  mcp_servers: { id: string; name: string; tools: string[] }[];
  knowledge_bases: { id: string; name: string }[];
  models: { id: string; provider: string; capabilities: { image: boolean; pdf: boolean; source: string } }[];
  agents: { id: string; name: string }[];
  default_model: string | null;
}

export interface ArchitectAgentSpec {
  name: string;
  id?: string;
  description?: string;
  instructions: string;
  model?: string;
  tools?: string[];
  mcp_tools?: { server: string; tool: string }[];
  knowledge_bases?: string[];
  memory?: boolean;
  model_settings?: { temperature?: number; max_tokens?: number };
}

export interface DraftResult {
  spec: ArchitectAgentSpec;
  assumptions: string[];
  dropped: { kind: string; id: string }[];
  run_id: string;
  model: string;
}

export interface SaveWarning {
  code: string;
  path: string;
  message: string;
}

export interface SaveResult {
  id: string;
  agent_id: string;
  version: number;
  published: boolean;
  warnings: SaveWarning[];
}

export class ArchitectError extends ApiError {
  /** The API's `code` ("choose_model", "version_mismatch", "tool_not_allowed", ...), when it sent one. */
  public code: string | null;
  /** Every other field of the error body (`latest_version`, `tools`, `providers`, `problems`). */
  public extra: Record<string, unknown>;

  constructor(status: number, message: string, code: string | null, extra: Record<string, unknown>) {
    super(status, message);
    this.name = "ArchitectError";
    this.code = code;
    this.extra = extra;
  }
}

async function call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${BASE_URL}/api/v1/architect${path ? `/${path}` : ""}`, {
    method,
    headers: { "Content-Type": "application/json", ...apiHeaders() },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (response.ok) return response.json();
  let message = `HTTP ${response.status}`;
  let code: string | null = null;
  let extra: Record<string, unknown> = {};
  try {
    const data = (await response.json()) as Record<string, unknown>;
    const { detail, code: rawCode, ...rest } = data;
    if (typeof detail === "string") message = detail;
    else if (Array.isArray(detail)) message = detail.map((item) => String((item as { msg?: unknown }).msg ?? "")).join(", ");
    if (typeof rawCode === "string") code = rawCode;
    extra = rest;
  } catch {
    // Not JSON: keep the status text.
  }
  throw new ArchitectError(response.status, message, code, extra);
}

export const getArchitect = () => call<ArchitectState>("GET", "");
export const getArchitectOptions = () => call<ArchitectOptions>("GET", "options");
/** Idempotent. Rejects with code `choose_model` when no model is available. */
export const seedArchitect = () => call<{ architect_agent_id: string; drafter_agent_id: string; model: string }>("POST", "");
export const draftAgent = (description: string, current?: ArchitectAgentSpec) =>
  call<DraftResult>("POST", "draft", current ? { description, current } : { description });
export const saveAgent = (spec: ArchitectAgentSpec, options: { publish: boolean; expectedVersion?: number | null }) =>
  call<SaveResult>("POST", "agents", {
    spec,
    publish: options.publish,
    ...(options.expectedVersion != null ? { expected_version: options.expectedVersion } : {}),
  });

// ------------------------------------------------------------------------------------- form

export interface AgentFormValues {
  name: string;
  id: string;
  description: string;
  instructions: string;
  model: string;
  tools: string[];
  mcp_tools: { server: string; tool: string }[];
  knowledge_bases: string[];
  memory: boolean;
  temperature: string;
  max_tokens: string;
}

export const EMPTY_FORM: AgentFormValues = {
  name: "",
  id: "",
  description: "",
  instructions: "",
  model: "",
  tools: [],
  mcp_tools: [],
  knowledge_bases: [],
  memory: false,
  temperature: "",
  max_tokens: "",
};

export function specToForm(spec: ArchitectAgentSpec): AgentFormValues {
  return {
    name: spec.name ?? "",
    id: spec.id ?? "",
    description: spec.description ?? "",
    instructions: spec.instructions ?? "",
    model: spec.model ?? "",
    tools: [...(spec.tools ?? [])],
    mcp_tools: (spec.mcp_tools ?? []).map((ref) => ({ ...ref })),
    knowledge_bases: [...(spec.knowledge_bases ?? [])],
    memory: spec.memory === true,
    temperature: spec.model_settings?.temperature == null ? "" : String(spec.model_settings.temperature),
    max_tokens: spec.model_settings?.max_tokens == null ? "" : String(spec.model_settings.max_tokens),
  };
}

export function formToSpec(form: AgentFormValues): ArchitectAgentSpec {
  const settings: NonNullable<ArchitectAgentSpec["model_settings"]> = {};
  const temperature = form.temperature.trim();
  const maxTokens = form.max_tokens.trim();
  if (temperature !== "" && Number.isFinite(Number(temperature))) settings.temperature = Number(temperature);
  if (maxTokens !== "" && Number.isFinite(Number(maxTokens))) settings.max_tokens = Math.trunc(Number(maxTokens));
  return {
    name: form.name.trim(),
    ...(form.id.trim() ? { id: form.id.trim() } : {}),
    description: form.description.trim(),
    instructions: form.instructions,
    ...(form.model ? { model: form.model } : {}),
    tools: form.tools,
    mcp_tools: form.mcp_tools,
    knowledge_bases: form.knowledge_bases,
    memory: form.memory,
    ...(Object.keys(settings).length > 0 ? { model_settings: settings } : {}),
  };
}

/** A stored agent spec (provider/name model object, memory config) as a form's spec. */
export function storedSpecToArchitect(stored: Record<string, unknown>, slug: string): ArchitectAgentSpec {
  const model = stored.model as { provider?: string; name?: string } | undefined;
  const settings = stored.model_settings as { temperature?: number | null; max_tokens?: number | null } | undefined;
  return {
    name: String(stored.name ?? ""),
    id: slug,
    description: typeof stored.description === "string" ? stored.description : "",
    instructions: String(stored.instructions ?? ""),
    ...(model?.provider && model?.name ? { model: `${model.provider}:${model.name}` } : {}),
    tools: Array.isArray(stored.tools) ? (stored.tools as string[]) : [],
    mcp_tools: Array.isArray(stored.mcp_tools) ? (stored.mcp_tools as { server: string; tool: string }[]) : [],
    knowledge_bases: Array.isArray(stored.knowledge_bases) ? (stored.knowledge_bases as string[]) : [],
    memory: stored.memory != null && stored.memory !== false,
    ...(settings && (settings.temperature != null || settings.max_tokens != null)
      ? { model_settings: { ...(settings.temperature != null ? { temperature: settings.temperature } : {}), ...(settings.max_tokens != null ? { max_tokens: settings.max_tokens } : {}) } }
      : {}),
  };
}
