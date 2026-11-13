import { beforeEach, describe, expect, it, vi } from "vitest";
import { formatOutcomeCount, loadOutcomeCounts, OUTCOME_COUNT_LIMIT, OUTCOME_WINDOW_MS } from "./processorData";
import { processors } from "@/lib/processors";

vi.mock("@/lib/processors", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/processors")>();
  return { ...actual, processors: { catalog: vi.fn(), test: vi.fn(), results: vi.fn() } };
});

describe("outcome counts", () => {
  beforeEach(() => vi.clearAllMocks());

  it("formats a count, a full page and an unknown count", () => {
    expect(formatOutcomeCount({ count: 12, more: false })).toBe("12");
    expect(formatOutcomeCount({ count: 200, more: true })).toBe("200+");
    expect(formatOutcomeCount(undefined)).toBe("—");
  });

  it("asks once per type for the last 24 h and leaves out a type whose call fails", async () => {
    const now = Date.parse("2026-11-14T12:00:00Z");
    vi.mocked(processors.results).mockImplementation(async (filters) => {
      if (filters?.type === "max_length") throw new Error("boom");
      return { items: filters?.type === "pii_redact" ? [{} as never, {} as never] : [], next_before: filters?.type === "pii_redact" ? "c" : null };
    });
    const counts = await loadOutcomeCounts(["pii_redact", "max_length", "moderation"], now);
    expect(counts).toEqual({ pii_redact: { count: 2, more: true }, moderation: { count: 0, more: false } });
    expect(processors.results).toHaveBeenCalledTimes(3);
    expect(processors.results).toHaveBeenCalledWith({ type: "pii_redact", from: new Date(now - OUTCOME_WINDOW_MS).toISOString(), limit: OUTCOME_COUNT_LIMIT }, undefined);
  });
});
