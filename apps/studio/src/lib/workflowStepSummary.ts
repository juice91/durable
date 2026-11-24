import type { WorkflowStep } from "./workflows";

/** What a step does and how, in one line: a tool id or an agent name, then a compact input summary. */
export interface StepSummary {
  kind: "tool" | "agent";
  /** The tool id, or the agent's name (the id when the agents list is not loaded). */
  lead: string;
  /** The agent's id, for the link to its page. */
  agentId?: string;
  /** The input summary: `GET host/path · extract_html links`, `v3 · input: query`. May be empty. */
  detail: string;
  /** The tool's or agent's name and the detail as one string (the canvas node's subtitle). */
  text: string;
}

const MAX_EXPRESSION = 48;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function cut(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** An input value as one short token: `$ref` as its path, `$template` as `template(...)`, a string as it is. */
export function inputValueText(value: unknown): string {
  if (typeof value === "string") return value;
  if (isRecord(value)) {
    if (typeof value.$ref === "string") return value.$ref;
    if ("$template" in value) return "template(...)";
  }
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function keys(value: unknown): string[] {
  return isRecord(value) ? Object.keys(value) : [];
}

/** `host/path` of a url, without scheme, query or fragment; a non-URL (a `$ref`, a template) as its token text. */
function hostPath(value: unknown): string {
  const text = inputValueText(value);
  try {
    const url = new URL(text);
    const path = url.pathname === "/" ? "" : url.pathname;
    return `${url.host}${path}`;
  } catch {
    return text;
  }
}

function httpDetail(input: Record<string, unknown>): string {
  const method = typeof input.method === "string" ? input.method.toUpperCase() : null;
  const target = input.url !== undefined ? hostPath(input.url) : "";
  const parts = [[method, target].filter(Boolean).join(" ")];
  for (const reducer of ["extract_html", "response_query", "parse_feed"] as const) {
    const config = input[reducer];
    if (config === undefined) continue;
    const mode = isRecord(config) && typeof config.mode === "string" ? ` ${config.mode}` : "";
    parts.push(`${reducer}${mode}`);
  }
  return parts.filter(Boolean).join(" · ");
}

function returnDetail(input: Record<string, unknown>): string {
  const parts: string[] = [];
  const value = input.value;
  if (isRecord(value) && "$template" in value) {
    const template = value.$template;
    parts.push(isRecord(template) ? `returns {${Object.keys(template).join(", ")}}` : "returns template(...)");
  } else if (isRecord(value) && typeof value.$ref === "string") {
    parts.push(`returns ${value.$ref}`);
  } else if (isRecord(value)) {
    parts.push(`returns {${Object.keys(value).join(", ")}}`);
  } else if (value !== undefined) {
    parts.push(`returns ${cut(inputValueText(value), MAX_EXPRESSION)}`);
  }
  if (input.outcome !== undefined) parts.push(`outcome ${inputValueText(input.outcome)}`);
  return parts.join(" · ");
}

function toolDetail(toolId: string, input: Record<string, unknown>): string {
  switch (toolId) {
    case "http.request":
      return httpDetail(input);
    case "json.query":
      return input.expression === undefined ? "" : cut(inputValueText(input.expression), MAX_EXPRESSION);
    case "workflow.return":
      return returnDetail(input);
    case "workflow.history": {
      const where = keys(input.where);
      return [input.select !== undefined ? `select ${inputValueText(input.select)}` : "", where.length > 0 ? `where ${where.join(", ")}` : ""]
        .filter(Boolean)
        .join(" · ");
    }
    case "text.check": {
      const rules = keys(input.rules);
      return rules.length > 0 ? rules.join(", ") : "";
    }
    default:
      return Object.keys(input).join(", ");
  }
}

/** The step's "does" summary; null for a step kind that has none (a branch lists its cases, a loop its bound). */
export function stepSummary(step: WorkflowStep, agentNames?: Readonly<Record<string, string>>): StepSummary | null {
  if (step.type === "tool") {
    const detail = toolDetail(step.tool_id, isRecord(step.input) ? step.input : {});
    return { kind: "tool", lead: step.tool_id, detail, text: detail ? `${step.tool_id} ${detail}` : step.tool_id };
  }
  if (step.type === "agent") {
    const lead = agentNames?.[step.agent_id] ?? step.agent_id;
    const inputKeys = keys(step.input);
    const detail = [step.version ? `v${step.version}` : "latest", inputKeys.length > 0 ? `input: ${inputKeys.join(", ")}` : ""].filter(Boolean).join(" · ");
    return { kind: "agent", lead, agentId: step.agent_id, detail, text: `${lead} ${detail}` };
  }
  return null;
}
