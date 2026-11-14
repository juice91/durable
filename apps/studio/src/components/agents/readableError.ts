// Follow-up 1b: the agent editor shows API validation messages in plain words. Routing a message to
// its step (errorTarget, stepForError) always uses the raw message; only what is displayed passes
// through `readableError`.

/** Spec keys as the editor names them (an index after a list key becomes "<label> <n + 1>"). */
const FIELD_LABELS: Record<string, string> = {
  name: "Name",
  model: "Model",
  provider: "Provider",
  model_settings: "Model settings",
  instructions: "Instructions",
  tools: "Tool",
  mcp_tools: "MCP tool",
  input_processors: "Input processor",
  output_processors: "Output processor",
  context_fields: "Request context field",
  knowledge: "Knowledge",
  knowledge_bases: "Knowledge base",
  memory: "Memory",
  working_memory: "Working memory",
  semantic_recall: "Semantic recall",
  output: "Output",
  config: "Config",
};

/** The first key of a location the API reports for an agent spec. */
const SPEC_KEYS = new Set(Object.keys(FIELD_LABELS).concat(["body", "spec", "draft_spec"]));

/** "input_processors.0.config.model", "input_processors[0].config" or ["body", "name"] in words. */
export function readablePath(path: string | ReadonlyArray<string | number>): string {
  const tokens = (typeof path === "string" ? path.replace(/\[(\d+)\]/g, ".$1").split(".") : path.map(String))
    .map((t) => t.trim())
    .filter(Boolean);
  while (tokens.length > 1 && ["body", "spec", "draft_spec"].includes(tokens[0])) tokens.shift();
  const parts: string[] = [];
  for (const token of tokens) {
    if (/^\d+$/.test(token) && parts.length > 0) {
      parts[parts.length - 1] += ` ${Number(token) + 1}`;
    } else {
      parts.push(FIELD_LABELS[token] ?? token.replace(/_/g, " "));
    }
  }
  const text = parts.join(" › ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const PATH = String.raw`[a-z_][a-z0-9_]*(?:\[\d+\]|\.[a-z0-9_]+)*`;

function isSpecPath(path: string): boolean {
  return SPEC_KEYS.has(path.split(/[.[]/)[0]);
}

/**
 * An API error message in plain words: pydantic's "Value error, " / "Assertion failed, " prefixes
 * dropped, spec locations ("input_processors[0].config.model: ...", "(name: ...)", Python loc tuples
 * like "('body', 'name')") named as the editor names them. Anything else is returned unchanged.
 */
export function readableError(message: string): string {
  let text = message.replace(/(^|[(:,;]\s*)(?:Value error|Assertion failed), /g, "$1");
  // Python loc tuples: ('body', 'input_processors', 0, 'config')
  text = text.replace(/\((?:'[^']*'|\d+)(?:, (?:'[^']*'|\d+))+,?\)/g, (tuple) => {
    const parts = tuple.slice(1, -1).split(",").map((p) => p.trim().replace(/^'|'$/g, "")).filter(Boolean);
    return isSpecPath(parts[0] ?? "") ? readablePath(parts) : tuple;
  });
  // A location before a colon, at the start or inside parentheses.
  text = text.replace(new RegExp(String.raw`(^|\()(${PATH}): `, "g"), (whole, lead: string, path: string) =>
    isSpecPath(path) ? `${lead}${readablePath(path)}: ` : whole,
  );
  return text.charAt(0).toUpperCase() + text.slice(1);
}
