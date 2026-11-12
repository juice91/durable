import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";

export type JsonSchema = {
  type?: "object" | "string" | "number" | "integer" | "boolean" | "array";
  title?: string;
  description?: string;
  enum?: Array<string | number>;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  default?: unknown;
};

export interface ToolDefinition {
  id: string;
  title: string;
  description: string;
  side_effect: boolean;
  external_idempotent: boolean;
  input_schema: JsonSchema;
  allowed: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseTool(value: unknown): ToolDefinition {
  if (!isRecord(value)
    || typeof value.id !== "string"
    || typeof value.title !== "string"
    || typeof value.description !== "string"
    || typeof value.side_effect !== "boolean"
    || typeof value.external_idempotent !== "boolean"
    || !isRecord(value.input_schema)
    || typeof value.allowed !== "boolean") {
    throw new Error("Invalid tool catalog response");
  }
  return value as unknown as ToolDefinition;
}

async function request(path: string): Promise<unknown> {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json", ...apiHeaders() },
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export async function listTools(): Promise<ToolDefinition[]> {
  const data = await request("/api/v1/tools");
  if (!Array.isArray(data)) throw new Error("Invalid tool catalog response");
  return data.map(parseTool);
}

export async function getTool(id: string): Promise<ToolDefinition> {
  return parseTool(await request(`/api/v1/tools/${encodeURIComponent(id)}`));
}
