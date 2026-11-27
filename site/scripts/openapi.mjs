// Renders the API's OpenAPI schema (written by dump-openapi.py) as one reference page per route group.
// Adapted from the renderer of the earlier documentation-site proposal.

const METHODS = ["get", "post", "put", "patch", "delete"];
const TITLES = {
  "ag-ui": "AG-UI",
  "api-keys": "API keys",
  iam: "Identity and access",
  "kb-sources": "Knowledge-base sources",
  "local-auth": "Local sign-in",
  "mcp-catalog": "MCP catalog",
  "mcp-servers": "MCP servers",
  otlp: "OTLP ingest",
};

export function tagTitle(tag) {
  if (TITLES[tag]) return TITLES[tag];
  const words = tag.replace(/-/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export const tagSlug = (tag) => tag.toLowerCase().replace(/[^a-z0-9]+/g, "-");

const refName = (ref) => ref.split("/").pop();

/** A compact type string for a schema: object fields one level deep, named models beyond that. */
export function shape(spec, schema, depth = 0) {
  if (!schema) return "any";
  if (schema.$ref) {
    const target = spec.components?.schemas?.[refName(schema.$ref)];
    return depth >= 1 || !target ? refName(schema.$ref) : shape(spec, target, depth + 1);
  }
  const alternatives = schema.anyOf ?? schema.oneOf;
  if (alternatives) return alternatives.map((s) => shape(spec, s, depth)).join(" | ");
  if (schema.enum) return schema.enum.map((v) => JSON.stringify(v)).join(" | ");
  if (schema.const !== undefined) return JSON.stringify(schema.const);
  if (schema.type === "array") return `${shape(spec, schema.items, depth)}[]`;
  if (schema.type === "object" && schema.properties) {
    const required = new Set(schema.required ?? []);
    const fields = Object.entries(schema.properties).map(([k, v]) => `${k}${required.has(k) ? "" : "?"}: ${shape(spec, v, depth + 1)}`);
    return `{ ${fields.join(", ")} }`;
  }
  return schema.type ?? "any";
}

const resolve = (spec, schema) => (schema?.$ref ? spec.components?.schemas?.[refName(schema.$ref)] : schema);
const cell = (text) => String(text).replace(/\\/g, "\\\\").replace(/\|/g, "\\|").replace(/\n/g, " ");
const code = (text) => `\`${cell(text).replace(/`/g, "'")}\``;
/** Docstrings are prose, not Markdown: keep the first paragraph and neutralise markup characters. */
const prose = (text) =>
  (text ?? "").trim().split(/\n\s*\n/)[0].replace(/\s*\n\s*/g, " ").replace(/``/g, "`").replace(/[<>]/g, (c) => (c === "<" ? "&lt;" : "&gt;")).replace(/[\\[]/g, "\\$&");

function operation(spec, path, method, op) {
  const out = [`## ${op.summary || `${method.toUpperCase()} ${path}`}`, "", `\`${method.toUpperCase()} ${path}\``, ""];
  if (op.description) out.push(prose(op.description), "");
  const params = (op.parameters ?? []).filter((p) => p.name.toLowerCase() !== "authorization");
  if (params.length) {
    out.push("| Parameter | In | Type | Required |", "| --- | --- | --- | --- |");
    for (const p of params) out.push(`| ${code(p.name)} | ${p.in} | ${code(shape(spec, p.schema))} | ${p.required ? "yes" : "no"} |`);
    out.push("");
  }
  const body = resolve(spec, op.requestBody?.content?.["application/json"]?.schema);
  if (body?.properties) {
    const required = new Set(body.required ?? []);
    out.push("Request body (JSON):", "", "| Field | Type | Required |", "| --- | --- | --- |");
    for (const [name, schema] of Object.entries(body.properties)) out.push(`| ${code(name)} | ${code(shape(spec, schema, 1))} | ${required.has(name) ? "yes" : "no"} |`);
    out.push("");
  }
  const codes = Object.keys(op.responses ?? {}).filter((c) => c !== "422");
  const list = [...codes.map((c) => `\`${c}\``), ...(op.responses?.["422"] ? ["`422` (validation error)"] : [])];
  if (list.length) out.push(`Responses: ${list.join(", ")}.`, "");
  return out.join("\n");
}

export function renderOpenApi(spec) {
  const groups = new Map();
  for (const [path, item] of Object.entries(spec.paths)) {
    for (const method of METHODS) {
      const op = item[method];
      if (!op) continue;
      const tag = op.tags?.[0] ?? "other";
      if (!groups.has(tag)) groups.set(tag, []);
      groups.get(tag).push({ path, method, op });
    }
  }
  return [...groups.entries()]
    .map(([tag, ops]) => ({
      slug: `reference/api/${tagSlug(tag)}`,
      label: tagTitle(tag),
      title: `${tagTitle(tag)} API`,
      description: `The ${tagTitle(tag)} routes of the control-plane API.`,
      operations: ops.length,
      body: [
        "Generated from the API's OpenAPI schema. Which credential a route accepts (an API key with the right scope, or a signed-in user's token) depends on the route and on your [identity settings](IAM_URL).",
        "",
        ...ops.map((o) => operation(spec, o.path, o.method, o.op)),
      ].join("\n"),
    }))
    .sort((a, b) => a.label.localeCompare(b.label));
}
