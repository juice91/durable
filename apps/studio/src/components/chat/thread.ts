import type { ThreadMessage } from "@assistant-ui/react";

// One definition of the event shapes: the canonical types live in lib/runs.ts.
import type { PartialTextMap, RunEvent } from "@/lib/runs";

export type { PartialTextMap, RunEvent };

// Convert a run's events and partial text to assistant-ui thread messages. `idPrefix` keeps message
// ids unique when several runs (conversation turns) share one thread.
export function runToThread(events: RunEvent[] = [], partialText: PartialTextMap, idPrefix = ""): {
  messages: ThreadMessage[];
} {
  const messages: ThreadMessage[] = [];
  const standaloneToolResults = new Map<string, unknown>();
  const embeddedToolCallIds = new Set<string>();
  for (const event of events) {
    if (event.type === "model_message") {
      const data = event.data as {
        tool_calls?: Array<{ tool_call_id?: string }>;
      };
      for (const call of data.tool_calls ?? []) {
        if (call.tool_call_id) embeddedToolCallIds.add(call.tool_call_id);
      }
    }
    if (event.type !== "tool_result") continue;
    const data = event.data as {
      tool_call_id?: string;
      result?: unknown;
      content?: unknown;
    };
    if (data.tool_call_id) {
      standaloneToolResults.set(
        data.tool_call_id,
        data.content !== undefined ? data.content : data.result,
      );
    }
  }

  let userMessage: ThreadMessage | null = null;
  let currentModelRequest: {
    modelRequest: number;
    text: string;
    toolCalls: Array<{
      tool_call_id: string;
      tool_name: string;
      args: any;
      result?: any;
    }>;
    toolResults: Array<{
      tool_call_id: string;
      result: any;
    }>;
  } | null = null;

  for (const event of events) {
    if (event.type === "run_started") {
      const inputData = event.data as { input: string };
      const inputText = typeof inputData.input === "string" ? inputData.input : JSON.stringify(inputData.input);

      userMessage = {
        id: `${idPrefix}user-${event.seq}`,
        createdAt: new Date(event.ts),
        role: "user" as const,
        content: [{ type: "text" as const, text: inputText }],
        attachments: [],
        metadata: {
          custom: {},
          isOptimistic: false,
        },
      };
      messages.push(userMessage);
    } else if (event.type === "model_message") {
      const eventData = event.data as {
        model_request: number;
        text?: string;
        tool_calls?: Array<{
          tool_call_id: string;
          tool_name: string;
          args: any;
          result?: any;
          is_partial?: boolean;
        }>;
        is_final: boolean;
      };

      const modelReqNum = eventData.model_request;

      // If this is a new model request, save the previous one first
      if (currentModelRequest && currentModelRequest.modelRequest !== modelReqNum) {
        finalizeAssistantMessage();
      }

      // Initialize new model request if needed
      if (!currentModelRequest || currentModelRequest.modelRequest !== modelReqNum) {
        currentModelRequest = {
          modelRequest: modelReqNum,
          text: "",
          toolCalls: [],
          toolResults: [],
        };
      }

      // Handle tool calls
      if (eventData.tool_calls) {
        for (const toolCall of eventData.tool_calls) {
          currentModelRequest.toolCalls.push(toolCall);
        }
      }

      // Handle text - Rule 4: partial text is transient, replaced by model_message
      if (eventData.text) {
        if (eventData.is_final) {
          // Final message replaces partial text completely (Rule 4)
          currentModelRequest.text = eventData.text;
        } else {
          // Partial text - use from partialText map (Rule 4: discard when model_message arrives)
          const partialKey = String(modelReqNum);
          if (partialText && partialText[partialKey]) {
            currentModelRequest.text = partialText[partialKey].text;
          } else {
            currentModelRequest.text += eventData.text;
          }
        }
      }

      // If this is final, finalize the assistant message
      if (eventData.is_final) {
        finalizeAssistantMessage();
        currentModelRequest = null;
      }
    } else if (event.type === "tool_call") {
      const toolCall = event.data as {
        tool_call_id: string;
        tool_name: string;
        args?: Record<string, any>;
      };
      if (embeddedToolCallIds.has(toolCall.tool_call_id)) continue;
      const result = standaloneToolResults.get(toolCall.tool_call_id);
      messages.push({
        id: `${idPrefix}assistant-tool-${toolCall.tool_call_id}`,
        createdAt: new Date(event.ts),
        role: "assistant",
        content: [{
          type: "tool-call",
          toolCallId: toolCall.tool_call_id,
          toolName: toolCall.tool_name,
          args: toolCall.args ?? {},
          argsText: JSON.stringify(toolCall.args ?? {}),
          ...(result !== undefined ? { result } : {}),
        }],
        status: { type: "complete", reason: "stop" },
        metadata: {
          custom: {},
          unstable_annotations: [],
          unstable_data: [],
          unstable_state: null,
          steps: [],
        },
      });
    } else if (event.type === "tool_result") {
      // Record tool result to be added as model content
      const toolResultData = event.data as {
        tool_call_id: string;
        result: any;
      };
      if (currentModelRequest) {
        currentModelRequest.toolResults.push(toolResultData);
      }
    }
  }

  // Finalize the last assistant message if it exists
  if (currentModelRequest) {
    finalizeAssistantMessage();
  }

  function finalizeAssistantMessage() {
    if (!currentModelRequest) return;

    // Build content array with all parts
    const content: ThreadMessage["content"] = [
      // Add tool call parts (each tool call followed by its result as model content)
      ...currentModelRequest.toolCalls.map((toolCall, i) => {
        const result = currentModelRequest.toolResults[i];
        return [{
          type: "tool-call" as const,
          toolCallId: toolCall.tool_call_id,
          toolName: toolCall.tool_name,
          args: toolCall.args || {},
          argsText: JSON.stringify(toolCall.args || {}),
          ...(result ? { result: result.result } : {}),
        } as const, ...(result ? [{ type: "text" as const, text: JSON.stringify(result.result, null, 2) } as const] : [])];
      }).flat(),
      // Add final text response (only if there's content)
      ...(currentModelRequest.text ? [{ type: "text" as const, text: currentModelRequest.text } as const] : []),
    ];

    if (content.length === 0) return;

    // Create the final assistant message with correct content type
    const finalMessage: ThreadMessage = {
      id: `${idPrefix}assistant-${currentModelRequest.modelRequest}`,
      createdAt: new Date(),
      role: "assistant" as const,
      content,
      status: { type: "complete" as const, reason: "stop" as const },
      metadata: {
        custom: {},
        unstable_annotations: [],
        unstable_data: [],
        unstable_state: null,
        steps: [],
      },
    };
    messages.push(finalMessage);
  }

  return { messages };
}

/** One conversation turn: a run, the message that started it, its durable events and live partials. */
export type ThreadTurn = {
  runId: string;
  input: string;
  events: RunEvent[];
  partials?: PartialTextMap;
};

// Convert a conversation's turns, in order, to one assistant-ui thread. A turn whose run_started event
// has not arrived yet shows its input as the user message, so a sent message appears at once.
export function conversationToThread(turns: ThreadTurn[]): { messages: ThreadMessage[] } {
  const messages: ThreadMessage[] = [];
  for (const turn of turns) {
    const prefix = `${turn.runId}:`;
    if (!turn.events.some((event) => event.type === "run_started")) {
      messages.push({
        id: `${prefix}user-pending`,
        createdAt: new Date(),
        role: "user",
        content: [{ type: "text", text: turn.input }],
        attachments: [],
        metadata: { custom: {} },
      });
    }
    messages.push(...runToThread(turn.events, turn.partials ?? {}, prefix).messages);
  }
  return { messages };
}
