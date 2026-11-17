import { describe, expect, it } from "vitest";
import type { AgentSpec } from "@/lib/api";
import { draftFromSpec, draftToSpec } from "./editorDraft";

const base: AgentSpec = { spec_version: 1, name: "a", instructions: "hi", model: { provider: "test", name: "echo" }, tools: [], model_settings: { temperature: null, max_tokens: null } };

describe("editor draft step and stream processors", () => {
  it("carries both lists through the draft and back unchanged", () => {
    const stored = {
      ...base,
      step_processors: [{ type: "pii_redact", config: { kinds: ["email"], action: "redact" } }],
      stream_processors: [{ type: "regex_blocklist", config: { patterns: ["x"], action: "block" } }],
    } as AgentSpec;
    const draft = draftFromSpec(stored);
    expect(draft.stepProcessors).toHaveLength(1);
    expect(draft.streamProcessors[0].type).toBe("regex_blocklist");
    expect(draftToSpec(draft, stored)).toEqual(stored);
  });

  it("leaves empty lists out of the saved spec", () => {
    const saved = draftToSpec(draftFromSpec(base), base) as unknown as Record<string, unknown>;
    expect("step_processors" in saved).toBe(false);
    expect("stream_processors" in saved).toBe(false);
  });

  it("owns the two keys, so a later step cannot set them through extra", () => {
    const saved = draftToSpec({ ...draftFromSpec(base), extra: { stream_processors: [{ type: "pii_redact", config: {} }] } }, base) as unknown as Record<string, unknown>;
    expect("stream_processors" in saved).toBe(false);
  });
});
