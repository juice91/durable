import { describe, expect, it } from "vitest";
import type { AgentSpec } from "@/lib/api";
import {
  contextFieldErrors,
  defaultProcessorConfig,
  errorTarget,
  placeholderErrors,
  placeholders,
  sectionsDiffer,
  sectionsOf,
  withSpecSections,
} from "./specSections";
import { modelsFixture, processorCatalogFixture } from "./specSections.fixtures";

const base: AgentSpec = { spec_version: 1, name: "a", instructions: "hi", model: { provider: "test", name: "echo" }, tools: [] };
const empty = { input_processors: [], output_processors: [], context_fields: [] };

describe("withSpecSections", () => {
  it("leaves every empty list out of the spec", () => {
    const spec = withSpecSections({ ...base, input_processors: [], context_fields: [] } as AgentSpec, empty);
    expect(Object.keys(spec).sort()).toEqual(["instructions", "model", "name", "spec_version", "tools"]);
  });

  it("writes the lists in the server's dump shape", () => {
    const spec = withSpecSections(base, {
      input_processors: [{ type: "regex_blocklist", config: { patterns: ["a", "", "b"], action: "block" } }],
      output_processors: [],
      context_fields: [
        { name: "tier", type: "enum", values: ["gold", "", "silver"], max_length: 5 },
        { name: "note", type: "string", max_length: 10, values: ["x"] },
      ],
    });
    expect(spec.input_processors).toEqual([{ type: "regex_blocklist", config: { patterns: ["a", "b"], action: "block" } }]);
    expect("output_processors" in spec).toBe(false);
    expect(spec.context_fields).toEqual([
      { name: "tier", type: "enum", required: false, description: "", max_length: null, values: ["gold", "silver"] },
      { name: "note", type: "string", required: false, description: "", max_length: 10, values: [] },
    ]);
  });
});

describe("sectionsDiffer", () => {
  it("ignores key order and dump defaults, sees real changes", () => {
    const stored = sectionsOf({ ...base, context_fields: [{ name: "a", type: "string", required: false, description: "", max_length: null, values: [] }] } as AgentSpec);
    expect(sectionsDiffer({ ...empty, context_fields: [{ type: "string", name: "a" }] }, stored)).toBe(false);
    expect(sectionsDiffer({ ...empty, context_fields: [{ type: "string", name: "a", required: true }] }, stored)).toBe(true);
    expect(sectionsDiffer({ ...stored, input_processors: [{ type: "pii_redact", config: {} }] }, stored)).toBe(true);
  });
});

describe("placeholders", () => {
  it("mirrors eap_schemas placeholder_errors", () => {
    const text = "Hi {{context.tier}} {{ context.tier }} {{context.region}} {{context.Bad}} {{context}}";
    expect(placeholders(text)).toEqual(["tier", "region"]);
    expect(placeholderErrors(text, [{ name: "tier", type: "string" }])).toEqual([
      "malformed placeholder {{context.Bad}}: use {{context.<field name>}}",
      "malformed placeholder {{context}}: use {{context.<field name>}}",
      "placeholder {{context.region}} names no context field",
    ]);
    expect(placeholderErrors("{{context.tier}}", [{ name: "tier", type: "string" }])).toEqual([]);
  });
});

describe("contextFieldErrors", () => {
  it("checks names, reserved names, duplicates and enum values", () => {
    const fields = [{ name: "tier", type: "string" as const }, { name: "tier", type: "string" as const }];
    expect(contextFieldErrors(fields[0], 0, fields)).toEqual([]);
    expect(contextFieldErrors(fields[1], 1, fields)).toEqual(["Duplicate field name 'tier'"]);
    expect(contextFieldErrors({ name: "user_id", type: "string" }, 0, [])[0]).toMatch(/reserved/);
    expect(contextFieldErrors({ name: "1x", type: "string" }, 0, [])[0]).toMatch(/lowercase letter/);
    const enumField = { name: "e", type: "enum" as const, values: [""] };
    expect(contextFieldErrors(enumField, 0, [enumField])).toEqual(["An enum field needs at least one value"]);
  });
});

describe("defaultProcessorConfig", () => {
  it("uses the schema defaults and picks test:moderation for moderation", () => {
    const byId = Object.fromEntries(processorCatalogFixture.items.map((d) => [d.id, d]));
    expect(defaultProcessorConfig(byId.pii_redact, [])).toEqual({ kinds: ["email", "phone", "credit_card", "iban", "ipv4", "us_ssn"], action: "redact" });
    expect(defaultProcessorConfig(byId.regex_blocklist, [])).toEqual({ patterns: [], action: "block" });
    expect(defaultProcessorConfig(byId.max_length, [])).toEqual({ action: "block" });
    expect(defaultProcessorConfig(byId.moderation, modelsFixture).model).toEqual({ provider: "test", name: "moderation" });
  });
});

describe("errorTarget", () => {
  it("routes API messages to the form section they name", () => {
    expect(errorTarget("output_processors[1].config.patterns[0]: invalid")).toEqual({ kind: "processor", phase: "output", index: 1 });
    expect(errorTarget("instructions: placeholder {{context.x}} names no context field")).toEqual({ kind: "instructions" });
    expect(errorTarget("processor model 'x:y' is not allowed on this platform")).toEqual({ kind: "processors" });
    expect(errorTarget("Agent not found")).toBeNull();
  });
});
