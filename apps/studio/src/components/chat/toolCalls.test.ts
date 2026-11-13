import { describe, expect, it } from "vitest";
import type { RunEvent, RunEventData } from "@/lib/runs";
import { formatDuration, toolCallInfos } from "./toolCalls";
import { draftDiffersFromLatest } from "./runOptions";
import type { AgentSpec, AgentVersion } from "@/lib/api";

const event = (seq: number, type: RunEvent["type"], data: object, ts: string): RunEvent => ({ seq, type, data: data as RunEventData, ts });

describe("toolCallInfos", () => {
  it("reads status and duration from tool_call and tool_result events", () => {
    const infos = toolCallInfos(
      [
        event(1, "tool_call", { tool_call_id: "a", tool_name: "x" }, "2026-11-14T10:00:00.000Z"),
        event(2, "tool_result", { tool_call_id: "a", status: "ok" }, "2026-11-14T10:00:01.250Z"),
        event(3, "tool_call", { tool_call_id: "b", tool_name: "y" }, "2026-11-14T10:00:02.000Z"),
        event(4, "tool_result", { tool_call_id: "b", status: "error" }, "2026-11-14T10:00:02.040Z"),
        event(5, "tool_call", { tool_call_id: "c", tool_name: "z" }, "2026-11-14T10:00:03.000Z"),
        event(6, "tool_result", { tool_call_id: "c", status: "no_result" }, "2026-11-14T10:00:03.000Z"),
      ],
      false,
    );
    expect(infos.get("a")).toEqual({ status: "ok", durationMs: 1250 });
    expect(infos.get("b")).toEqual({ status: "error", durationMs: 40 });
    expect(infos.get("c")).toEqual({ status: "no_result", durationMs: 0 });
  });

  it("takes the start of a call embedded in a model message, and leaves a missing result running or unanswered", () => {
    const events = [
      event(1, "model_message", { model_request: 0, tool_calls: [{ tool_call_id: "m", tool_name: "x", args: {} }, "junk"] }, "2026-11-14T10:00:00Z"),
      event(2, "tool_result", { tool_call_id: "m" }, "2026-11-14T10:00:02Z"),
      event(3, "tool_call", { tool_call_id: "open", tool_name: "y" }, "2026-11-14T10:00:03Z"),
    ];
    expect(toolCallInfos(events, true).get("m")).toEqual({ status: "ok", durationMs: 2000 });
    expect(toolCallInfos(events, true).get("open")).toEqual({ status: "running", durationMs: null });
    expect(toolCallInfos(events, false).get("open")).toEqual({ status: "no_result", durationMs: null });
  });

  it("gives no duration for unparsable or out-of-order times", () => {
    const infos = toolCallInfos(
      [
        event(1, "tool_call", { tool_call_id: "a" }, "not a time"),
        event(2, "tool_result", { tool_call_id: "a", status: "ok" }, "2026-11-14T10:00:00Z"),
        event(3, "tool_call", { tool_call_id: "b" }, "2026-11-14T10:00:05Z"),
        event(4, "tool_result", { tool_call_id: "b", status: "ok" }, "2026-11-14T10:00:00Z"),
      ],
      false,
    );
    expect(infos.get("a")?.durationMs).toBeNull();
    expect(infos.get("b")?.durationMs).toBeNull();
  });
});

describe("formatDuration", () => {
  it("formats ms, seconds and minutes", () => {
    expect(formatDuration(40)).toBe("40 ms");
    expect(formatDuration(1250)).toBe("1.3 s");
    expect(formatDuration(12_400)).toBe("12 s");
    expect(formatDuration(125_000)).toBe("2 min 5 s");
    expect(formatDuration(120_000)).toBe("2 min");
  });
});

describe("draftDiffersFromLatest", () => {
  const spec = (instructions: string, extra: object = {}): AgentSpec =>
    ({ spec_version: 1, name: "A", instructions, model: { provider: "p", name: "m" }, tools: [], ...extra }) as AgentSpec;
  const version = (n: number, s: AgentSpec): AgentVersion => ({ agent_id: "a", version: n, spec: s, spec_hash: "h", created_at: "", created_by: "" });

  it("is true without a published version, false while versions load", () => {
    expect(draftDiffersFromLatest(spec("x"), null, null)).toBe(true);
    expect(draftDiffersFromLatest(spec("x"), 1, null)).toBe(false);
  });

  it("compares the draft with the latest version, ignoring key order and undefined keys", () => {
    const latest = spec("same", { model_settings: { temperature: 0.2 } });
    const reordered = { model_settings: { temperature: 0.2 }, tools: [], model: { name: "m", provider: "p" }, instructions: "same", name: "A", spec_version: 1, description: undefined } as unknown as AgentSpec;
    expect(draftDiffersFromLatest(reordered, 2, [version(1, spec("old")), version(2, latest)])).toBe(false);
    expect(draftDiffersFromLatest(spec("changed"), 2, [version(2, latest)])).toBe(true);
  });
});
