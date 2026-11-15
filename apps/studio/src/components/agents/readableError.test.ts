import { describe, expect, it } from "vitest";
import { readableError, readablePath } from "./readableError";

// Follow-up 1b: API validation messages in the agent editor, in plain words.

describe("readableError", () => {
  it.each([
    ["Value error, name cannot be empty", "Name cannot be empty"],
    ["Value error, working memory is invalid at a: unsupported type 'date'", "Working memory is invalid at a: unsupported type 'date'"],
    ["Assertion failed, at most 8 knowledge bases per agent", "At most 8 knowledge bases per agent"],
    ["input_processors[1].config.patterns[0]: invalid RE2 pattern", "Input processor 2 › Config › patterns 1: invalid RE2 pattern"],
    ["memory.semantic_recall: embedding model 'litellm:x' is not allowed", "Memory › Semantic recall: embedding model 'litellm:x' is not allowed"],
    ["tools[0]: unknown tool", "Tool 1: unknown tool"],
    [
      "The draft is not a valid agent spec (output_processors.0.config.model: Value error, provider cannot be empty); fix it in the editor",
      "The draft is not a valid agent spec (Output processor 1 › Config › Model: provider cannot be empty); fix it in the editor",
    ],
    ["Field required ('body', 'name')", "Field required Name"],
    ["String should have at most 120 characters, Value error, name cannot be empty", "String should have at most 120 characters, name cannot be empty"],
  ])("%s", (raw, readable) => {
    expect(readableError(raw)).toBe(readable);
  });

  it("leaves messages without spec locations alone (apart from the first letter)", () => {
    expect(readableError("observer model 'test:nope' is not allowed on this platform")).toBe(
      "Observer model 'test:nope' is not allowed on this platform",
    );
    expect(readableError("Agent not found")).toBe("Agent not found");
    expect(readableError("http: something: else")).toBe("Http: something: else");
    expect(readableError("Failed to save draft")).toBe("Failed to save draft");
  });

  it("names loc arrays", () => {
    expect(readablePath(["body", "input_processors", 0, "config"])).toBe("Input processor 1 › Config");
    expect(readablePath("context_fields[2].name")).toBe("Request context field 3 › Name");
  });
});
