import { describe, it, expect } from "vitest";
import { conversationToThread, runToThread } from "./thread";
import type { ThreadMessage } from "@assistant-ui/react";

// Type definitions matching the runs.ts exports
type RunEvent = {
  seq: number;
  type: "run_started" | "model_message" | "tool_call" | "tool_result" | "run_completed" | "run_failed";
  data: unknown;
  ts: string;
};

type PartialTextMap = {
  [modelRequest: string]: {
    text: string;
    attempt: string;
  };
};

// Helper to create events
const mkEvent = (type: RunEvent["type"], data: unknown, seq: number = 1): RunEvent => ({
  seq,
  type,
  data,
  ts: "2025-01-01T00:00:00.000Z",
});

const mkPartialText = (modelRequest: number, text: string, attempt: string): PartialTextMap => ({
  [String(modelRequest)]: { text, attempt },
});

describe("runToThread", () => {
  it("maps user input and model message to thread with text-only content", () => {
    const events: RunEvent[] = [
      mkEvent("run_started", { input: "Hello" }),
      mkEvent("model_message", { model_request: 1, text: "Hi there!", is_final: true }),
      mkEvent("run_completed", {}),
    ];

    const partialText: PartialTextMap = {};

    const thread = runToThread(events, partialText);

    expect(thread.messages).toHaveLength(2);
    expect(thread.messages[0].role).toBe("user");
    expect(thread.messages[0].content).toHaveLength(1);
    expect(thread.messages[0].content[0]).toMatchObject({
      type: "text",
      text: "Hello",
    });

    expect(thread.messages[1].role).toBe("assistant");
    expect(thread.messages[1].content).toHaveLength(1);
    expect(thread.messages[1].content[0]).toMatchObject({
      type: "text",
      text: "Hi there!",
    });
  });

  it("maps tool call and tool result to assistant message parts", () => {
    const events: RunEvent[] = [
      mkEvent("run_started", { input: "Calculate 2+2" }),
      mkEvent("model_message", {
        model_request: 1,
        tool_calls: [
          {
            tool_call_id: "tool_1",
            tool_name: "calculator",
            args: { operation: "add", a: 2, b: 2 },
            is_partial: false,
          },
        ],
        is_final: false,
      }),
      mkEvent("tool_call", {
        tool_call_id: "tool_1",
        tool_name: "calculator",
        args: { operation: "add", a: 2, b: 2 },
      }),
      mkEvent("tool_result", {
        tool_call_id: "tool_1",
        result: 4,
      }),
      mkEvent("model_message", {
        model_request: 1,
        text: "The result is 4",
        is_final: true,
      }),
      mkEvent("run_completed", {}),
    ];

    const partialText: PartialTextMap = {};

    const thread = runToThread(events, partialText);

    expect(thread.messages).toHaveLength(2);
    expect(thread.messages[1].role).toBe("assistant");
    expect(thread.messages[1].content).toHaveLength(3); // tool_call + tool_call + text

    // First part should be tool_call
    expect(thread.messages[1].content[0]).toMatchObject({
      type: "tool-call",
      toolCallId: "tool_1",
      toolName: "calculator",
    });

    // Second part should be the tool result (model content)
    expect(thread.messages[1].content[1]).toMatchObject({
      type: "text",
      text: JSON.stringify(4, null, 2),
    });

    // Third part should be the final text
    expect(thread.messages[1].content[2]).toMatchObject({
      type: "text",
      text: "The result is 4",
    });
  });

  it("maps standalone durable tool events to one tool-call part with its result", () => {
    const events: RunEvent[] = [
      mkEvent("run_started", { input: "Open a ticket" }, 0),
      mkEvent("model_message", { model_request: 0, text: "" }, 1),
      mkEvent("tool_call", {
        tool_call_id: "call-1",
        tool_name: "tickets__create",
        args: { title: "Printer broken" },
      }, 2),
      mkEvent("tool_result", {
        tool_call_id: "call-1",
        tool_name: "tickets__create",
        status: "ok",
        content: { ticket_id: "TCK-42" },
      }, 3),
      mkEvent("model_message", {
        model_request: 1,
        text: "Created **TCK-42**",
      }, 4),
    ];

    const thread = runToThread(events, {});
    const assistantParts = thread.messages
      .filter((message) => message.role === "assistant")
      .flatMap((message) => message.content);

    expect(assistantParts).toContainEqual(expect.objectContaining({
      type: "tool-call",
      toolCallId: "call-1",
      toolName: "tickets__create",
      args: { title: "Printer broken" },
      result: { ticket_id: "TCK-42" },
    }));
  });

  it("handles partial text that is replaced on a new attempt", () => {
    const events: RunEvent[] = [
      mkEvent("run_started", { input: "Say hello" }),
      mkEvent("model_message", {
        model_request: 1,
        text: "H",
        is_final: false,
      }),
    ];

    const partialText = mkPartialText(1, "Hello", "attempt-1");

    const thread = runToThread(events, partialText);

    // Partial text from the map should be used since no model_message with is_final=true
    expect(thread.messages[1].content[0]).toMatchObject({
      type: "text",
      text: "Hello",
    });
  });

  it("replaces partial text with model_message when it arrives", () => {
    const events: RunEvent[] = [
      mkEvent("run_started", { input: "Say hello" }),
      mkEvent("model_message", {
        model_request: 1,
        text: "H",
        is_final: false,
      }),
      mkEvent("model_message", {
        model_request: 1,
        text: "Hello there!",
        is_final: true,
      }),
    ];

    const partialText = mkPartialText(1, "H", "attempt-1");

    const thread = runToThread(events, partialText);

    // The final model_message should replace the partial text
    expect(thread.messages[1].content[0]).toMatchObject({
      type: "text",
      text: "Hello there!",
    });
  });

  it("does not include partial text for completed requests", () => {
    const events: RunEvent[] = [
      mkEvent("run_started", { input: "Test" }),
      mkEvent("model_message", {
        model_request: 1,
        text: "Completed",
        is_final: true,
      }),
      mkEvent("run_completed", {}),
    ];

    const partialText = mkPartialText(1, "Old partial", "attempt-old");

    const thread = runToThread(events, partialText);

    // The partial text should be discarded because model_message with is_final=true arrived
    expect(thread.messages[1].content[0]).toMatchObject({
      type: "text",
      text: "Completed",
    });
  });

  it("handles multiple tool calls in sequence", () => {
    const events: RunEvent[] = [
      mkEvent("run_started", { input: "Do two calculations" }),
      mkEvent("model_message", {
        model_request: 1,
        tool_calls: [
          {
            tool_call_id: "tool_1",
            tool_name: "calculator",
            args: { operation: "add", a: 2, b: 2 },
            is_partial: false,
          },
          {
            tool_call_id: "tool_2",
            tool_name: "calculator",
            args: { operation: "multiply", a: 3, b: 4 },
            is_partial: false,
          },
        ],
        is_final: false,
      }),
      mkEvent("tool_result", {
        tool_call_id: "tool_1",
        result: 4,
      }),
      mkEvent("tool_result", {
        tool_call_id: "tool_2",
        result: 12,
      }),
      mkEvent("model_message", {
        model_request: 1,
        text: "The results are 4 and 12",
        is_final: true,
      }),
    ];

    const partialText: PartialTextMap = {};

    const thread = runToThread(events, partialText);

    expect(thread.messages[1].content).toHaveLength(5); // 2 tool_calls + 2 tool_results + text
    expect(thread.messages[1].content[0]).toMatchObject({ type: "tool-call", toolName: "calculator" });
    expect(thread.messages[1].content[1]).toMatchObject({ type: "text", text: JSON.stringify(4, null, 2) });
    expect(thread.messages[1].content[2]).toMatchObject({ type: "tool-call", toolName: "calculator" });
    expect(thread.messages[1].content[3]).toMatchObject({ type: "text", text: JSON.stringify(12, null, 2) });
    expect(thread.messages[1].content[4]).toMatchObject({ type: "text", text: "The results are 4 and 12" });
  });

  it("handles model_message with no text field", () => {
    const events: RunEvent[] = [
      mkEvent("run_started", { input: "Test" }),
      mkEvent("model_message", {
        model_request: 1,
        tool_calls: [
          {
            tool_call_id: "tool_1",
            tool_name: "calculator",
            args: { operation: "add", a: 2, b: 2 },
            is_partial: false,
          },
        ],
        is_final: true,
      }),
      mkEvent("tool_result", {
        tool_call_id: "tool_1",
        result: 4,
      }),
    ];

    const partialText: PartialTextMap = {};

    const thread = runToThread(events, partialText);

    // Should have only tool-call part, no text part since there's no text
    expect(thread.messages[1].content).toHaveLength(1);
    expect(thread.messages[1].content[0]).toMatchObject({ type: "tool-call" });
  });
});

describe("conversationToThread", () => {
  const turn = (runId: string, input: string, answer?: string) => ({
    runId,
    input,
    events: answer === undefined
      ? []
      : [
          mkEvent("run_started", { input }, 0),
          mkEvent("model_message", { model_request: 0, text: answer }, 1),
          mkEvent("run_completed", { output: answer }, 2),
        ],
  });

  it("puts every turn in one thread, in order, with ids unique across turns", () => {
    const { messages } = conversationToThread([
      turn("run-1", "My name is Ada.", "Hi Ada"),
      turn("run-2", "What is my name?", "Ada"),
    ]);
    const texts = messages.map((m) => [m.role, (m.content[0] as { text: string }).text]);
    expect(texts).toEqual([
      ["user", "My name is Ada."],
      ["assistant", "Hi Ada"],
      ["user", "What is my name?"],
      ["assistant", "Ada"],
    ]);
    expect(new Set(messages.map((m) => m.id)).size).toBe(messages.length);
  });

  it("shows a just-sent turn's input before its run_started event arrives", () => {
    const { messages } = conversationToThread([turn("run-1", "One", "Uno"), turn("run-2", "Two")]);
    expect(messages).toHaveLength(3);
    expect(messages[2].role).toBe("user");
    expect((messages[2].content[0] as { text: string }).text).toBe("Two");
  });
});
