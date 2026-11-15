import { describe, expect, it } from "vitest";
import type { AgentSpec } from "@/lib/api";
import {
  approvalProblems,
  builderFromSchema,
  costProblem,
  fallbackProblems,
  isApprovalError,
  isIdentityCapabilityError,
  isOutputError,
  limitsProblems,
  limitsValue,
  normaliseUsd,
  mcpApprovalSuggestion,
  newBuilderField,
  outputProblems,
  outputSchemaProblem,
  outputStatus,
  outputValue,
  prunedApprovals,
  publishWarningsOf,
  schemaFromBuilder,
  withBuilderType,
  withCitationsField,
} from "./agentCapabilities";
import { canonicalJson, stepForErrorMessage, stepStatus } from "./specSections";
import { draftFromSpec, draftToSpec } from "./stepper/editorDraft";
import { EDITOR_STEPS, missingFields, stepForError } from "./stepper/registry";

const BASE = {
  spec_version: 1,
  name: "Bot",
  instructions: "Help",
  model: { provider: "test", name: "echo" },
  tools: ["clock.now", "tickets.create"],
  mcp_tools: [{ server: "docs", tool: "search" }],
  model_settings: { temperature: null, max_tokens: null },
} as AgentSpec;

const withKeys = (extra: Record<string, unknown>) => ({ ...BASE, ...extra }) as unknown as AgentSpec;

describe("output schema subset (client mirror)", () => {
  const ok = { type: "object", properties: { a: { type: "string" } }, required: ["a"] };
  it.each([
    ["$ref", { type: "object", properties: { a: { $ref: "#/x" } } }, "unsupported keyword '$ref'"],
    ["pattern", { type: "object", properties: { a: { type: "string", pattern: "x" } } }, "unsupported keyword 'pattern'"],
    ["anyOf", { type: "object", anyOf: [] }, "unsupported keyword 'anyOf'"],
    ["non-object root", { type: "array", items: { type: "string" } }, 'the root must have "type": "object"'],
    ["required undeclared", { type: "object", properties: {}, required: ["x"] }, '"required" may only name'],
    ["bad name", { type: "object", properties: { "a b": { type: "string" } } }, "property names must match"],
    ["unparsable text", "{", "not valid JSON"],
  ])("rejects %s", (_label, schema, message) => {
    expect(outputSchemaProblem(schema)).toContain(message);
  });

  it("accepts the subset", () => {
    expect(outputSchemaProblem(ok)).toBeNull();
    expect(outputSchemaProblem({ type: "object", properties: { n: { type: ["integer", "null"], minimum: 0 }, e: { enum: ["a", "b"] }, l: { type: "array", items: { type: "object", properties: {} } } } })).toBeNull();
  });

  it("depth 6 and 101 properties are rejected", () => {
    let deep: Record<string, unknown> = { type: "string" };
    for (let i = 0; i < 5; i += 1) deep = { type: "object", properties: { x: deep } };
    expect(outputSchemaProblem(deep)).toBeNull(); // five objects deep
    deep = { type: "object", properties: { x: deep } };
    expect(outputSchemaProblem(deep)).toContain("nested at most 5");
    const many = Object.fromEntries(Array.from({ length: 101 }, (_, i) => [`p${i}`, { type: "string" }]));
    expect(outputSchemaProblem({ type: "object", properties: many })).toContain("at most 100 properties");
  });
});

describe("field builder", () => {
  it("round-trips a schema it can show (keywords without a column are kept)", () => {
    const schema = {
      type: "object",
      title: "Answer",
      additionalProperties: false,
      properties: {
        summary: { type: "string", description: "Short", maxLength: 200 },
        level: { type: "string", enum: ["low", "high"] },
        score: { type: ["number", "null"], minimum: 0, maximum: 1 },
        tags: { type: "array", items: { type: "string", format: "uri" }, maxItems: 3 },
        owner: { type: "object", properties: { id: { type: "integer" } }, required: ["id"] },
        rows: { type: "array", items: { type: "object", properties: { k: { type: "boolean" } } } },
      },
      required: ["summary", "level"],
    };
    const root = builderFromSchema(schema)!;
    expect(root.fields.map((f) => f.name)).toEqual(["summary", "level", "score", "tags", "owner", "rows"]);
    expect(canonicalJson(schemaFromBuilder(root))).toBe(canonicalJson(schema));
  });

  it("schemas it cannot show are raw-only", () => {
    expect(builderFromSchema({ type: "object", properties: { a: { enum: ["x"] } } })).toBeNull();
    expect(builderFromSchema({ type: "object", properties: { a: { type: "array", items: { type: "array", items: { type: "string" } } } } })).toBeNull();
    expect(builderFromSchema("{")).toBeNull();
  });

  it("blank and duplicate names stay in the schema and break the subset (no row is dropped)", () => {
    const a = { ...newBuilderField("a") };
    const dup = { ...newBuilderField("a") };
    const schema = schemaFromBuilder({ fields: [a, dup, newBuilderField("")], rest: {}, keepRequired: false });
    expect(Object.keys(schema.properties as object)).toEqual(["a", "a#2", ""]);
    expect(outputSchemaProblem(schema)).toContain("property names must match");
  });

  it("a type change drops keywords of the old type", () => {
    const field = { ...newBuilderField("a"), rest: { maxLength: 3, title: "T" }, enumValues: ["x"] };
    const next = withBuilderType(field, "number");
    expect(next.rest).toEqual({ title: "T" });
    expect(next.enumValues).toBeNull();
  });
});

describe("citations field", () => {
  it("is inserted once", () => {
    const once = withCitationsField({ type: "object", properties: { a: { type: "string" } } }) as { properties: Record<string, unknown> };
    expect(once.properties.citations).toEqual({ type: "array", description: "Source numbers [n] used", items: { type: "integer" } });
    expect(withCitationsField(once)).toBe(once);
  });
});

describe("values as saved", () => {
  it("unset fields are dropped, and empty limits remove the key", () => {
    expect(outputValue({ json_schema: { type: "object" }, mode: undefined, name: "", description: null, max_retries: undefined })).toEqual({ json_schema: { type: "object" } });
    expect(limitsValue({ max_requests: undefined, max_cost_usd: "" })).toBeUndefined();
    expect(limitsValue({ max_requests: 5 })).toEqual({ max_requests: 5 });
  });

  it("an untouched agent with all four features saves the same JSON", () => {
    const stored = withKeys({
      output: { json_schema: { type: "object", properties: { a: { type: "string" } } }, mode: "native" },
      approvals: { tools: ["tickets.create", "docs/search"], timeout_hours: 2 },
      limits: { max_requests: 5, max_cost_usd: "0.5" },
      fallback_models: [{ provider: "test", name: "other" }],
    });
    expect(JSON.stringify(draftToSpec(draftFromSpec(stored), stored))).toBe(JSON.stringify(stored));
  });

  it("approvals of unselected tools are pruned, and the key goes with the last one", () => {
    const approvals = { tools: ["tickets.create", "docs/search"], timeout_hours: 3 };
    expect(prunedApprovals(approvals, ["tickets.create"], [])).toEqual({ tools: ["tickets.create"], timeout_hours: 3 });
    expect(prunedApprovals(approvals, [], [])).toBeUndefined();
  });
});

describe("problems", () => {
  it("limits: ranges, and the cost as a 4-decimal amount", () => {
    expect(limitsProblems(withKeys({ limits: { max_requests: 201 } }))[0].field).toBe("max_requests");
    expect(costProblem("0.5")).toBeNull();
    expect(costProblem("0.00001")).not.toBeNull();
    expect(costProblem("1001")).not.toBeNull();
    expect(costProblem("abc")).not.toBeNull();
  });

  it("fallback: unchosen, the primary, duplicates", () => {
    const spec = withKeys({ fallback_models: [{ provider: "", name: "" }, { provider: "test", name: "echo" }, { provider: "test", name: "x" }, { provider: "test", name: "x" }] });
    expect(fallbackProblems(spec).map((p) => p.field)).toEqual(["fallback-add", "fallback-0", "fallback-1", "fallback-3"]);
  });

  it("approvals: stale entries and the timeout range", () => {
    const spec = withKeys({ approvals: { tools: ["gone.tool"], timeout_hours: 169 } });
    expect(approvalProblems(spec).map((p) => p.field)).toEqual(["stale-0", "timeout_hours"]);
  });

  it("output problems block saving on the Output step", () => {
    const spec = withKeys({ output: { json_schema: { type: "object", properties: { a: { type: "string", pattern: "x" } } }, max_retries: 9 } });
    expect(outputProblems(spec).map((p) => p.field)).toEqual(["schema", "max_retries"]);
    const missing = missingFields(draftFromSpec(spec), spec).filter((m) => m.step === "output");
    expect(missing.map((m) => m.fieldId)).toEqual(["output-schema-problems", "output-max_retries"]);
  });
});

describe("steps, status lines and error routing", () => {
  it("Output is step 8, after Memory", () => {
    expect(EDITOR_STEPS.map((s) => s.id)).toEqual(["identity", "instructions", "tools", "request-context", "processors", "knowledge", "memory", "output"]);
  });

  it("status lines", () => {
    expect(outputStatus(BASE)).toEqual({ text: "Off", valid: true });
    expect(outputStatus(withKeys({ output: { json_schema: { type: "object", properties: { a: { type: "string" }, b: { type: "string" }, c: { type: "string" } } } } })).text).toBe("Tool mode · 3 fields");
    expect(stepStatus("identity", withKeys({ fallback_models: [{ provider: "test", name: "a" }, { provider: "test", name: "b" }] }))!.text).toBe("test:echo · fallback ×2");
    expect(stepStatus("tools", withKeys({ approvals: { tools: ["tickets.create"] } }))!.text).toBe("3 tools · 1 needs approval");
    expect(stepStatus("identity", BASE)!.text).toBe("test:echo");
  });

  it("a 422 location into a new key opens its step", () => {
    const loc = (key: string) => [["body", "spec", key, 0]];
    expect(stepForError("x", EDITOR_STEPS, loc("output"))).toBe("output");
    expect(stepForError("x", EDITOR_STEPS, loc("limits"))).toBe("identity");
    expect(stepForError("x", EDITOR_STEPS, loc("fallback_models"))).toBe("identity");
    expect(stepForError("x", EDITOR_STEPS, loc("approvals"))).toBe("tools");
  });

  it("messages without a location are routed by their prefix", () => {
    expect(stepForErrorMessage("fallback_models[0]: model 'x:y' is not allowed on this platform")).toBe("identity");
    expect(stepForErrorMessage("Value error, fallback_models[1]: duplicate fallback model: test:x")).toBe("identity");
    expect(stepForErrorMessage("Value error, approvals.tools[0]: 'x' is not a tool of this agent")).toBe("tools");
    expect(stepForError("Value error, approvals.tools[0]: 'x' is not a tool of this agent", EDITOR_STEPS, [["body", "spec"]])).toBe("tools");
    expect(isOutputError("output.json_schema: the JSON Schema is invalid")).toBe(true);
    // Memory comes before Output, but its matcher no longer claims another key's json_schema.
    expect(stepForError("output.json_schema: unsupported keyword '$ref'", EDITOR_STEPS)).toBe("output");
    expect(stepForError("memory.working.json_schema: unsupported keyword '$ref'", EDITOR_STEPS)).toBe("memory");
    expect(stepForError("Value error, set either template or json_schema, not both", EDITOR_STEPS)).toBe("memory");
    expect(isOutputError("output_processors[0]: x")).toBe(false);
    expect(isApprovalError("approvals.timeout_hours: x")).toBe(true);
    expect(isIdentityCapabilityError("limits.max_cost_usd: x")).toBe(true);
  });

  it("MCP tools are suggested for approval only on evidence from their annotations", () => {
    expect(mcpApprovalSuggestion(null)).toBeNull();
    expect(mcpApprovalSuggestion(undefined)).toBeNull();
    expect(mcpApprovalSuggestion({})).toBe("Not marked read-only");
    expect(mcpApprovalSuggestion({ readOnlyHint: true })).toBeNull();
    expect(mcpApprovalSuggestion({ readOnlyHint: true, destructiveHint: true })).toBe("Marked destructive");
  });
});

describe("publish warnings", () => {
  it("reads the response's warnings and ignores malformed ones", () => {
    const warning = { code: "cost_cap_unpriced_model", path: "model", message: "model: 'test:echo' has no price" };
    expect(publishWarningsOf({ version: 2, warnings: [warning, { code: 1 }] })).toEqual([warning]);
    expect(publishWarningsOf({ version: 2 })).toEqual([]);
  });
});

describe("review fixes (AC.13)", () => {
  it("normaliseUsd matches the server's normalise_usd", () => {
    expect(normaliseUsd("0.50")).toBe("0.5");
    expect(normaliseUsd("1.0")).toBe("1");
    expect(normaliseUsd("1000")).toBe("1000");
    expect(normaliseUsd("10")).toBe("10");
    expect(normaliseUsd("007.10")).toBe("7.1");
    expect(normaliseUsd("0.0001")).toBe("0.0001");
    expect(normaliseUsd("0.12340")).toBe("0.1234");
    expect(normaliseUsd("abc")).toBe("abc");
    // Trailing zeros past the 4th decimal are fine (the server accepts them); a 5th digit is not.
    expect(costProblem("0.12340")).toBeNull();
    expect(costProblem("0.12345")).not.toBeNull();
  });

  it("an edit in the builder leaves untouched sibling nodes byte-identical", () => {
    const schema = {
      type: "object",
      properties: {
        meta: { description: "kept order", type: "object" },
        a: { maxLength: 3, type: "string" },
      },
    };
    const root = builderFromSchema(schema)!;
    root.fields[1] = { ...root.fields[1], description: "changed" };
    const out = schemaFromBuilder(root) as { properties: Record<string, unknown> };
    expect(JSON.stringify(out.properties.meta)).toBe(JSON.stringify(schema.properties.meta));
    expect(out.properties.a).toEqual({ type: "string", description: "changed", maxLength: 3 });
    // Reverting the edit writes the loaded node back exactly.
    root.fields[1] = { ...root.fields[1], description: "" };
    expect(JSON.stringify((schemaFromBuilder(root) as { properties: Record<string, unknown> }).properties.a)).toBe(JSON.stringify(schema.properties.a));
  });

  it("subset messages use the server's wording", () => {
    expect(outputSchemaProblem({ type: "object", $schema: 1 })).toBe('the JSON Schema is invalid: "$schema" must be a string');
    expect(outputSchemaProblem({ type: "object", properties: { o: { type: "object", minimum: 1 } } })).toBe(
      'the JSON Schema is invalid at o: unsupported keyword "minimum" on an object',
    );
  });
});
