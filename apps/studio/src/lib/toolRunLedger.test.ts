import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { canonicalInputKey, MAX_NOTES, toolRunLedger } from "./toolRunLedger";

const entry = (toolId: string, input: unknown, runId: string) => ({
  toolId, toolTitle: toolId, inputKey: canonicalInputKey(input), runId, state: "uncertain" as const,
});

function quotaExceeded(): never {
  throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
}

describe("toolRunLedger", () => {
  beforeEach(() => toolRunLedger.reset());
  afterEach(() => vi.restoreAllMocks());

  it("keys inputs canonically by a fixed-size hash, so key order does not matter and the input is not stored", () => {
    const big = { text: "x".repeat(32 * 1024) };
    expect(canonicalInputKey({ b: 1, a: { d: 2, c: 3 } })).toBe(canonicalInputKey({ a: { c: 3, d: 2 }, b: 1 }));
    expect(canonicalInputKey({ a: 1 })).not.toBe(canonicalInputKey({ a: 2 }));
    expect(canonicalInputKey(big)).toMatch(/^h1:[0-9a-f]{28}$/);
    toolRunLedger.remember(entry("a", big, "run-big"));
    expect(window.sessionStorage.getItem("eap.studio.toolRuns.v1")!.length).toBeLessThan(500);
  });

  it("keeps entries per tool and input, and forget only clears the matching run id", () => {
    toolRunLedger.remember(entry("a", { x: 1 }, "run-a"));
    toolRunLedger.remember(entry("b", { x: 1 }, "run-b"));
    toolRunLedger.remember(entry("a", { x: 2 }, "run-a2"));

    toolRunLedger.forget("a", canonicalInputKey({ x: 1 }), "someone-else");
    expect(toolRunLedger.pending("a", canonicalInputKey({ x: 1 }))?.runId).toBe("run-a");
    toolRunLedger.forget("a", canonicalInputKey({ x: 2 }), "run-a2");
    expect(toolRunLedger.pending("a", canonicalInputKey({ x: 2 }))).toBeUndefined();
    expect(toolRunLedger.pending("b", canonicalInputKey({ x: 1 }))?.runId).toBe("run-b");
  });

  it("persists in sessionStorage and hydrates from it on a fresh load", () => {
    toolRunLedger.remember(entry("a", {}, "run-a"));
    toolRunLedger.addNote({ runId: "r1", toolId: "a", toolTitle: "A" });
    toolRunLedger.reloadFromStorage();
    expect(toolRunLedger.pending("a", canonicalInputKey({}))?.runId).toBe("run-a");
    expect(toolRunLedger.notes()).toEqual([{ runId: "r1", toolId: "a", toolTitle: "A" }]);
  });

  it("when setItem throws QuotaExceededError while getItem still works, the recorded id is still found", () => {
    toolRunLedger.remember(entry("old", {}, "run-old")); // an older successful write in storage
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(quotaExceeded);
    const getItem = vi.spyOn(Storage.prototype, "getItem");

    toolRunLedger.remember(entry("a", { n: 1 }, "run-new"));
    expect(setItem).toHaveBeenCalled();
    expect(toolRunLedger.pending("a", canonicalInputKey({ n: 1 }))?.runId).toBe("run-new");
    expect(toolRunLedger.pending("old", canonicalInputKey({}))?.runId).toBe("run-old");
    // getItem works and still holds the stale JSON, but lookups never go back to it.
    expect(window.sessionStorage.getItem("eap.studio.toolRuns.v1")).not.toContain("run-new");
    expect(getItem).toHaveBeenCalledTimes(1); // only the assertion above
  });

  it("falls back to memory when storage is unavailable altogether", () => {
    toolRunLedger.reloadFromStorage();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
    toolRunLedger.remember(entry("a", {}, "run-a"));
    expect(toolRunLedger.pending("a", canonicalInputKey({}))?.runId).toBe("run-a");
  });

  it("caps notes at the last MAX_NOTES but never evicts pending ids", () => {
    for (let i = 0; i < MAX_NOTES + 5; i += 1) toolRunLedger.addNote({ runId: `n${i}`, toolId: "a", toolTitle: "A" });
    for (let i = 0; i < 200; i += 1) toolRunLedger.remember(entry("a", { i }, `p${i}`));
    const notes = toolRunLedger.notes();
    expect(notes).toHaveLength(MAX_NOTES);
    expect(notes[0].runId).toBe("n5");
    expect(notes[notes.length - 1].runId).toBe(`n${MAX_NOTES + 4}`);
    expect(toolRunLedger.pending("a", canonicalInputKey({ i: 0 }))?.runId).toBe("p0");
    expect(toolRunLedger.pending("a", canonicalInputKey({ i: 199 }))?.runId).toBe("p199");
  });
});
