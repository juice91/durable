// RM.17: the knowledge and memory spec helpers (shapes, client-side checks mirroring eap_schemas,
// status lines, server error ownership, summaries).
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(), listEmbeddingModels: vi.fn() }));

import type { AgentSpec } from "@/lib/api";
import {
  embeddingModelIdProblem,
  acceptedPiiKinds,
  piiKindsFromCatalog,
  PII_KINDS,
  setAcceptedPiiKinds,
  filterProblem,
  inheritedRedactKinds,
  isKnowledgeError,
  isMemoryError,
  knowledgeBasesValue,
  knowledgeProblems,
  knowledgeRefSummary,
  knowledgeStatus,
  memoryFeatures,
  memoryProblems,
  memoryStatus,
  memorySummaryLines,
  memoryValue,
  newKnowledgeBaseRef,
  newObservational,
  newSemanticRecall,
  newWorking,
  STARTER_WORKING_MEMORY_SCHEMA,
  templateProblem,
  typedNumber,
  workingSchemaProblem,
} from "./knowledgeMemory";
import { specKeyOfLocation, stepForError } from "./stepper/registry";
import { processorCatalogFixture } from "./specSections.fixtures";

const KB1 = "11111111-1111-4111-8111-111111111111";
const KB2 = "22222222-2222-4222-8222-222222222222";
const base = { spec_version: 1, name: "a", instructions: "x", model: { provider: "test", name: "echo" }, tools: [] } as unknown as AgentSpec;
const withSpec = (extra: Record<string, unknown>) => ({ ...base, ...extra }) as unknown as AgentSpec;

describe("shapes written to the spec", () => {
  it("memory off is no key at all (undefined), never {}; on with nothing set is {}", () => {
    expect(memoryValue(null)).toBeUndefined();
    expect(memoryValue({})).toEqual({});
    expect(JSON.stringify(memoryValue({}))).toBe("{}");
  });

  it("fills defaults and drops unset optional fields, in the API's field order (its model_dump)", () => {
    const value = memoryValue({
      redact: { kinds: ["email"], action: "redact" },
      semantic_recall: { ...newSemanticRecall("test:hash"), min_score: undefined },
      history: { last_turns: undefined, max_tokens: undefined },
      working: { read_only: true, json_schema: undefined, template: "# T", scope: "conversation" },
      observational: newObservational({ provider: "test", name: "observer" }),
    });
    expect(JSON.stringify(value)).toBe(
      JSON.stringify({
        // A history config, even an empty one, dumps its default window (HistoryConfig.last_turns).
        history: { last_turns: 20 },
        working: { scope: "conversation", template: "# T", read_only: true },
        observational: { model: { provider: "test", name: "observer" }, observe_after_tokens: 12000, keep_recent_turns: 4, reflect_after_tokens: 8000 },
        semantic_recall: { embedding_model: "test:hash", top_k: 4, message_range: 1, scope: "resource" },
        redact: { kinds: ["email"], action: "redact" },
      }),
    );
  });

  it("new sub-configs use the server's dump shape (defaults explicit)", () => {
    expect(newWorking()).toEqual({ scope: "resource", read_only: false });
    expect(newKnowledgeBaseRef(KB1)).toEqual({ id: KB1, top_k: 8 });
    expect(newSemanticRecall(null).embedding_model).toBe("");
  });

  it("an empty knowledge base list removes the key; refs are reordered to id, top_k, min_score, filter", () => {
    expect(knowledgeBasesValue([])).toBeUndefined();
    expect(JSON.stringify(knowledgeBasesValue([{ filter: { a: 1 }, min_score: 0.5, top_k: 3, id: KB1 }]))).toBe(
      JSON.stringify([{ id: KB1, top_k: 3, min_score: 0.5, filter: { a: 1 } }]),
    );
  });

  it("typedNumber: empty leaves the key out, numbers parse, anything else stays as typed", () => {
    expect(typedNumber("")).toBeUndefined();
    expect(typedNumber("  ")).toBeUndefined();
    expect(typedNumber("12")).toBe(12);
    expect(typedNumber("0.5")).toBe(0.5);
    expect(typedNumber("abc")).toBe("abc");
  });
});

describe("knowledge checks (KnowledgeBaseRef, validate_filter)", () => {
  it("accepts a valid list", () => {
    expect(knowledgeProblems(withSpec({ knowledge_bases: [{ id: KB1, top_k: 20, min_score: 0, filter: { lang: "en", tag: ["a", "b"], n: 1, ok: true } }] }))).toEqual([]);
  });

  it.each([
    [{ id: KB1, top_k: 0 }, "0-top_k"],
    [{ id: KB1, top_k: 21 }, "0-top_k"],
    [{ id: KB1, top_k: 2.5 }, "0-top_k"],
    [{ id: KB1, top_k: "x" }, "0-top_k"],
    [{ id: KB1, min_score: 1.5 }, "0-min_score"],
    [{ id: KB1, min_score: -0.1 }, "0-min_score"],
    [{ id: "not-a-uuid" }, "0-id"],
    [{ id: KB1, filter: "{bad" }, "0-filter"],
  ])("flags %j at %s", (ref, field) => {
    expect(knowledgeProblems(withSpec({ knowledge_bases: [ref] })).map((p) => p.field)).toEqual([field]);
  });

  it("at most 8, and no duplicates", () => {
    const nine = Array.from({ length: 9 }, (_, i) => ({ id: `0000000${i}-0000-4000-8000-000000000000`, top_k: 8 }));
    expect(knowledgeProblems(withSpec({ knowledge_bases: nine })).map((p) => p.field)).toContain("list");
    expect(knowledgeProblems(withSpec({ knowledge_bases: [{ id: KB1 }, { id: KB1.toUpperCase() }] })).map((p) => p.field)).toEqual(["1-id"]);
  });

  it.each([
    [null, null],
    [{}, null],
    ["{x", "Filter is not valid JSON"],
    [[1], "Filter must be a JSON object"],
    [{ "bad key": 1 }, "Filter key 'bad key' must use only letters, digits, _ . - (at most 64 characters)"],
    [{ k: [] }, "filter.k: a list needs 1 to 32 values"],
    [{ k: Array(33).fill(1) }, "filter.k: a list needs 1 to 32 values"],
    [{ k: { nested: 1 } }, "filter.k: value must be a string, number or boolean"],
    [{ k: [null] }, "filter.k: value must be a string, number or boolean"],
    [{ k: "x".repeat(257) }, "filter.k: string longer than 256 characters"],
    [Object.fromEntries(Array.from({ length: 17 }, (_, i) => [`k${i}`, i])), "Filter has more than 16 keys"],
  ])("filterProblem(%j) is %j", (value, message) => {
    expect(filterProblem(value)).toBe(message);
  });

  it("status: None, a count, or problems", () => {
    expect(knowledgeStatus(base)).toEqual({ text: "None", valid: true });
    expect(knowledgeStatus(withSpec({ knowledge_bases: [{ id: KB1, top_k: 8 }] }))).toEqual({ text: "1 knowledge base", valid: true });
    expect(knowledgeStatus(withSpec({ knowledge_bases: [{ id: KB1 }, { id: KB2 }] }))).toEqual({ text: "2 knowledge bases", valid: true });
    expect(knowledgeStatus(withSpec({ knowledge_bases: [{ id: KB1, top_k: 99 }] }))).toEqual({ text: "1 problem", valid: false });
  });

  it("summary line", () => {
    expect(knowledgeRefSummary({ id: KB1, top_k: 5, min_score: 0.4, filter: { lang: "en", tag: ["a", "b"] } })).toBe("Top 5 · min score 0.4 · filter lang=en, tag=a|b");
    expect(knowledgeRefSummary({ id: KB1 })).toBe("Top 8");
  });
});

describe("the working memory schema subset (validate_working_memory_schema)", () => {
  it("accepts the starter schema and a rich valid one", () => {
    expect(workingSchemaProblem(STARTER_WORKING_MEMORY_SCHEMA)).toBeNull();
    expect(
      workingSchemaProblem({
        $schema: "https://json-schema.org/draft/2020-12/schema",
        type: "object",
        required: ["name"],
        additionalProperties: false,
        properties: {
          name: { type: "string", title: "Name", description: "x" },
          plan: { enum: ["free", "pro"], type: "string" },
          tags: { type: "array", items: { type: "string" } },
          profile: { type: "object", properties: { address: { type: "object", properties: { city: { type: "string" } } } } },
        },
      }),
    ).toBeNull();
  });

  it.each([
    ["", "Enter a JSON schema"],
    ["{", "Schema is not valid JSON"],
    [[], "The schema must be a JSON object"],
    [{ type: "array" }, 'The root must have "type": "object"'],
    [{ type: "object", $schema: 1 }, '"$schema" must be a string'],
    [{ type: "object", pattern: "x" }, "(root): unsupported keyword 'pattern'"],
    [{ type: "object", properties: { d: { type: "date" } } }, "d: unsupported type 'date'"],
    [{ type: "object", properties: { d: {} } }, 'd: a schema needs a "type" or an "enum"'],
    [{ type: "object", properties: { s: { type: "string", minLength: 1 } } }, "s: unsupported keyword 'minLength'"],
    [{ type: "object", properties: { e: { enum: [] } } }, 'e: "enum" must be a list of 1 to 64 values'],
    [{ type: "object", properties: { e: { enum: [1, 1] } } }, 'e: "enum" values must be unique'],
    [{ type: "object", properties: { e: { enum: ["a", 1], type: "string" } } }, 'e: "enum" values must all be of type string'],
    [{ type: "object", properties: { a: { type: "array" } } }, 'a: an array needs "items"'],
    [{ type: "object", properties: { a: { type: "array", items: { type: "object" } } } }, "a[]: array items must be a string, number, integer, boolean or enum"],
    [{ type: "object", properties: { a: { type: "object", properties: { b: { type: "object", properties: { c: { type: "object" } } } } } } }, "a.b.c: objects may be nested at most 3 deep"],
    [{ type: "object", additionalProperties: "no" }, '(root): "additionalProperties" must be true or false'],
    [{ type: "object", required: [1] }, '(root): "required" must be a list of property names'],
    [{ type: "object", properties: Object.fromEntries(Array.from({ length: 51 }, (_, i) => [`p${i}`, { type: "string" }])) }, "At most 50 properties in total"],
  ])("rejects %j: %s", (schema, message) => {
    expect(workingSchemaProblem(schema)).toBe(message);
  });

  it("caps the size at 8 KiB of compact JSON", () => {
    const schema = { type: "object", properties: { a: { type: "string", description: "é".repeat(4100) } } };
    expect(workingSchemaProblem(schema)).toBe("The schema must be at most 8192 bytes");
  });

  it("templates: unset is the default, blank and oversize are refused", () => {
    expect(templateProblem(undefined)).toBeNull();
    expect(templateProblem("# Notes")).toBeNull();
    expect(templateProblem("   ")).toMatch(/cannot be blank/);
    expect(templateProblem("x".repeat(4097))).toMatch(/at most 4096 bytes/);
  });
});

describe("memory checks and status", () => {
  it("off has no problems and the status Off", () => {
    expect(memoryProblems(base)).toEqual([]);
    expect(memoryStatus(base)).toEqual({ text: "Off", valid: true });
    expect(memoryStatus(withSpec({ memory: null }))).toEqual({ text: "Off", valid: true });
  });

  it("status lists the history window and the features on", () => {
    expect(memoryStatus(withSpec({ memory: {} }))).toEqual({ text: "History 20", valid: true });
    expect(
      memoryStatus(
        withSpec({
          memory: {
            history: { last_turns: 8 },
            working: newWorking(),
            observational: newObservational({ provider: "test", name: "observer" }),
            semantic_recall: newSemanticRecall("test:hash"),
          },
        }),
      ),
    ).toEqual({ text: "History 8 · Working · Observational · Recall", valid: true });
  });

  it.each([
    [{ history: { last_turns: 0 } }, "history-last_turns"],
    [{ history: { last_turns: 201 } }, "history-last_turns"],
    [{ history: { max_tokens: 0 } }, "history-max_tokens"],
    [{ working: { template: " " } }, "working-template"],
    [{ working: { json_schema: "{" } }, "working-schema"],
    [{ working: { template: "x", json_schema: { type: "object" } } }, "working-format"],
    [{ observational: newObservational(null) }, "observational-model"],
    [{ observational: { ...newObservational({ provider: "test", name: "o" }), observe_after_tokens: 999 } }, "observational-observe_after_tokens"],
    [{ observational: { ...newObservational({ provider: "test", name: "o" }), keep_recent_turns: 51 } }, "observational-keep_recent_turns"],
    [{ observational: { ...newObservational({ provider: "test", name: "o" }), reflect_after_tokens: 200001 } }, "observational-reflect_after_tokens"],
    [{ semantic_recall: newSemanticRecall(null) }, "recall-embedding_model"],
    [{ semantic_recall: { ...newSemanticRecall("test:hash"), top_k: 11 } }, "recall-top_k"],
    [{ semantic_recall: { ...newSemanticRecall("test:hash"), message_range: 4 } }, "recall-message_range"],
    [{ semantic_recall: { ...newSemanticRecall("test:hash"), min_score: 2 } }, "recall-min_score"],
    [{ redact: { kinds: [], action: "redact" } }, "redact-kinds"],
  ])("flags %j at %s", (memory, field) => {
    expect(memoryProblems(withSpec({ memory })).map((p) => p.field)).toEqual([field]);
    expect(memoryStatus(withSpec({ memory })).valid).toBe(false);
  });

  it.each([
    ["", "Choose an embedding model"],
    ["nocolon", "Embedding model must be provider:name"],
    ["Bad:x", "Embedding model must be provider:name"],
    ["litellm:", "Type an embedding model name"],
    ["litellm:*", "Type an embedding model name"],
    ["litellm:a b", "Embedding model name may not contain spaces, commas or ="],
    ["litellm:text-embedding-3-small", null],
  ])("embedding model id %j: %j", (id, message) => {
    expect(embeddingModelIdProblem(id)).toBe(message);
  });
});

describe("server errors route to the Knowledge and Memory steps", () => {
  it.each([
    [`knowledge base '${KB1}' does not exist in this tenant`, "knowledge"],
    ["Value error, knowledge base id must be a UUID", "knowledge"],
    ["Value error, at most 8 knowledge bases per agent", "knowledge"],
    [`Value error, duplicate knowledge base: ${KB1}`, "knowledge"],
    ["Value error, filter.lang: string longer than 256 characters", "knowledge"],
    ["Value error, metadata key must match ^[A-Za-z0-9_.-]{1,64}$", "knowledge"],
    ["memory.semantic_recall: embedding model 'x:y' is not allowed", "memory"],
    ["observer model 'test:nope' is not allowed on this platform (not in the EAP_ALLOWED_MODELS allowlist)", "memory"],
    ["the embedding model allowlist (EAP_ALLOWED_EMBEDDING_MODELS) is misconfigured", "memory"],
    ["Value error, working memory is invalid at a: unsupported type 'date'", "memory"],
    ["Value error, template cannot be blank", "memory"],
    ["Value error, set either template or json_schema, not both", "memory"],
    ["Value error, memory.redact must use action 'redact' (memory writes are never blocked)", "memory"],
    ["Value error, invalid embedding model id 'x': expected 'provider:name'", "memory"],
    ["Value error, tool name 'retrieve' is reserved for a platform tool", "tools"],
    ["tool name 'update_working_memory' is reserved for a platform tool", "tools"],
  ])("%s -> %s", (message, step) => {
    expect(stepForError(message)).toBe(step);
  });

  it("does not claim the other steps' messages", () => {
    for (const message of [
      "model 'x:y' is not allowed on this platform (not in the EAP_ALLOWED_MODELS allowlist)",
      "processor model 'x:y' is not allowed on this platform",
      "instructions: placeholder {{context.x}} names no context field",
      "MCP server 'docs' is disabled",
    ]) {
      expect(isKnowledgeError(message)).toBe(false);
      expect(isMemoryError(message)).toBe(false);
    }
  });
});

describe("summaries for the Config panel and the memory card", () => {
  it("memory card: History is 'all turns' without memory, the effective window with it", () => {
    expect(memoryFeatures(base).map((f) => [f.label, f.on])).toEqual([
      ["History", true],
      ["Working memory", false],
      ["Observational", false],
      ["Semantic recall", false],
    ]);
    expect(memoryFeatures(withSpec({ memory: {} }))[0].label).toBe("History 20 turns");
    expect(memoryFeatures(withSpec({ memory: { history: { last_turns: 8 }, working: newWorking() } })).map((f) => [f.label, f.on])).toEqual([
      ["History 8 turns", true],
      ["Working memory", true],
      ["Observational", false],
      ["Semantic recall", false],
    ]);
  });

  it("Config panel lines", () => {
    expect(memorySummaryLines(base)).toEqual([]);
    expect(
      memorySummaryLines(
        withSpec({
          memory: {
            history: { max_tokens: 4000 },
            working: { ...newWorking(), json_schema: { type: "object" }, read_only: true },
            observational: newObservational({ provider: "test", name: "observer" }),
            semantic_recall: { ...newSemanticRecall("test:hash"), scope: "conversation" },
            redact: { kinds: ["email", "phone"], action: "redact" },
          },
        }),
      ),
    ).toEqual([
      ["History", "Last 20 turns, at most 4,000 tokens"],
      ["Working memory", "JSON schema, per user, read-only"],
      ["Observational", "test:observer, observe after 12,000 tokens"],
      ["Semantic recall", "test:hash, top 4, same conversation"],
      ["Redaction", "email, phone"],
    ]);
  });

  it("inherited redaction kinds come from every input pii_redact processor", () => {
    expect(inheritedRedactKinds([])).toEqual([]);
    expect(
      inheritedRedactKinds([
        { type: "pii_redact", config: { kinds: ["email"], action: "block" } },
        { type: "max_length", config: {} },
        { type: "pii_redact", config: { kinds: ["phone", "email"] } },
      ]),
    ).toEqual(["email", "phone"]);
  });
});

describe("422 locations route before message text (review fix)", () => {
  it.each([
    // The observer model's ModelRef messages read like the agent model's; the loc says memory.
    [["body", "spec", "memory", "observational", "model", "provider"], "Value error, provider cannot be empty", "memory"],
    [["body", "spec", "memory", "observational", "model", "name"], "Value error, name cannot be empty", "memory"],
    [["body", "spec", "model", "name"], "Value error, name cannot be empty", "identity"],
    [["body", "spec", "knowledge_bases", 0, "top_k"], "Input should be less than or equal to 20", "knowledge"],
    [["body", "spec", "knowledge_bases"], "Tuple should have at most 8 items after validation, not 9", "knowledge"],
    [["body", "spec", "memory", "semantic_recall", "scope"], "Input should be 'resource' or 'conversation'", "memory"],
    [["body", "spec", "memory", "redact", "kinds", 0], "Input should be 'email', 'phone', 'credit_card', 'iban', 'ipv4' or 'us_ssn'", "memory"],
  ] as const)("%j %s -> %s", (loc, message, step) => {
    expect(stepForError(message, undefined, [loc])).toBe(step);
  });

  it("falls back to the text when no location belongs to a step", () => {
    expect(stepForError("Value error, name cannot be empty", undefined, [["body", "spec", "model", "name"]])).toBe("identity");
    expect(stepForError("memory.semantic_recall: embedding model 'x:y' is not allowed", undefined, [])).toBe("memory");
    expect(stepForError("Input should be a valid integer", undefined, [["query", "limit"]])).toBeNull();
  });

  it("specKeyOfLocation", () => {
    expect(specKeyOfLocation(["body", "spec", "memory", "history"])).toBe("memory");
    expect(specKeyOfLocation(["spec", "knowledge_bases", 0])).toBe("knowledge_bases");
    expect(specKeyOfLocation(["body", "other"])).toBeNull();
    expect(specKeyOfLocation([])).toBeNull();
  });
});

describe("PII kinds (review fix)", () => {
  it("the constant matches the processor catalog's pii_redact kinds (GET /api/v1/processors)", () => {
    expect(piiKindsFromCatalog(processorCatalogFixture)).toEqual([...PII_KINDS]);
  });

  it("falls back to the constant without a catalog", () => {
    expect(piiKindsFromCatalog(null)).toEqual([...PII_KINDS]);
    expect(piiKindsFromCatalog({ items: [] })).toEqual([...PII_KINDS]);
  });

  it("an unknown stored kind is a warning (soft); empty, duplicate and block are blockers", () => {
    const problem = (redact: unknown) => memoryProblems(withSpec({ memory: { redact } }))[0];
    expect(problem({ kinds: ["email", "passport"], action: "redact" })).toMatchObject({ field: "redact-kinds", soft: true });
    expect(problem({ kinds: [], action: "redact" }).soft).toBeUndefined();
    expect(problem({ kinds: ["email", "email"] }).soft).toBeUndefined();
    expect(problem({ action: "block" }).soft).toBeUndefined();
    expect(problem({})).toBeUndefined();
  });
});

describe("the API dump shape (review fix: clean after save)", () => {
  it("fills defaults of set sub-configs and drops nulls, keeping unknown keys last", () => {
    expect(
      JSON.stringify(
        memoryValue({
          history: { max_tokens: 100 },
          working: { template: null as unknown as string },
          observational: { model: { provider: "test", name: "o" }, extra_knob: 1 } as never,
          semantic_recall: { embedding_model: "test:hash", min_score: null },
          redact: {},
        }),
      ),
    ).toBe(
      JSON.stringify({
        history: { last_turns: 20, max_tokens: 100 },
        working: { scope: "resource", read_only: false },
        observational: { model: { provider: "test", name: "o" }, observe_after_tokens: 12000, keep_recent_turns: 4, reflect_after_tokens: 8000, extra_knob: 1 },
        semantic_recall: { embedding_model: "test:hash", top_k: 4, message_range: 1, scope: "resource" },
        redact: { kinds: [...PII_KINDS], action: "redact" },
      }),
    );
    expect(knowledgeBasesValue([{ id: KB1, filter: {}, min_score: null }])).toEqual([{ id: KB1, top_k: 8 }]);
  });

  it("typedNumber gives the field's default when cleared", () => {
    expect(typedNumber("", 8)).toBe(8);
    expect(typedNumber("", undefined)).toBeUndefined();
  });
});

describe("follow-ups: first location decides, catalog kinds", () => {
  afterEach(() => setAcceptedPiiKinds(null));

  it("a location in a form-owned key goes to its built-in step, never to Memory by the text", () => {
    // "duplicate PII kind" also matches Memory's text; at input_processors it is the Processors step's.
    expect(stepForError("Value error, duplicate PII kind: email", undefined, [["body", "spec", "input_processors", 0, "config", "kinds"]])).toBe("processors");
    expect(stepForError("Value error, duplicate PII kind: email", undefined, [["body", "spec", "output_processors", 1, "config", "kinds"]])).toBe("processors");
    expect(stepForError("Value error, duplicate PII kind: email", undefined, [["body", "spec", "memory", "redact", "kinds"]])).toBe("memory");
    expect(stepForError("Value error, duplicate PII kind: email")).toBe("memory");
  });

  it("a mixed 422 is routed by its first location (name first: Identity)", () => {
    const message = "Value error, name cannot be empty, Input should be less than or equal to 10";
    const name = ["body", "spec", "name"];
    const recall = ["body", "spec", "memory", "semantic_recall", "top_k"];
    expect(stepForError(message, undefined, [name, recall])).toBe("identity");
    expect(stepForError(message, undefined, [recall, name])).toBe("memory");
  });

  it.each([
    [["body", "spec", "model", "provider"], "identity"],
    [["body", "spec", "model_settings", "temperature"], "identity"],
    [["body", "spec", "instructions"], "instructions"],
    [["body", "spec", "tools", 0], "tools"],
    [["body", "spec", "mcp_tools", 0, "server"], "tools"],
    [["body", "spec", "context_fields", 0, "name"], "request-context"],
    [["body", "spec", "knowledge_bases", 0], "knowledge"],
  ] as const)("the built-in keys: %j -> %s", (loc, step) => {
    expect(stepForError("Input should be a valid string", undefined, [loc])).toBe(step);
  });

  it("a location no step owns falls back to the text", () => {
    expect(stepForError("memory.semantic_recall: x", undefined, [["body", "spec", "spec_version"]])).toBe("memory");
  });

  it("kinds are checked against the catalog's list once it is read", () => {
    const redact = (kinds: string[]) => memoryProblems(withSpec({ memory: { redact: { kinds, action: "redact" } } }))[0];
    expect(redact(["passport"])).toMatchObject({ soft: true });
    const catalog = structuredClone(processorCatalogFixture);
    (catalog.items.find((i) => i.id === "pii_redact")!.config_schema.properties!.kinds.items as { enum: string[] }).enum.push("passport");
    expect(piiKindsFromCatalog(catalog)).toContain("passport");
    expect(acceptedPiiKinds()).toContain("passport");
    expect(redact(["passport"])).toBeUndefined();
    expect(redact(["iban"])).toBeUndefined();
  });

  it("duplicates are reported even beside an unknown kind (blocking, not soft)", () => {
    const problem = memoryProblems(withSpec({ memory: { redact: { kinds: ["email", "nope", "email"], action: "redact" } } }))[0];
    expect(problem).toEqual({ field: "redact-kinds", message: "Each kind of personal data may be listed once" });
  });
});
