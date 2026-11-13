import { afterEach, describe, expect, it, vi } from "vitest";
import { isoToLocalInput, localInputToIso } from "./datetimeLocal";

describe("datetime-local helpers", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("reads a datetime-local value as local time and round-trips it", () => {
    const iso = localInputToIso("2026-11-13T10:30")!;
    expect(iso).toMatch(/Z$/);
    expect(iso).toBe(new Date(2026, 10, 13, 10, 30).toISOString());
    expect(isoToLocalInput(iso)).toBe("2026-11-13T10:30");
    expect(localInputToIso("2026-11-13T10:30:15")).toBe(new Date(2026, 10, 13, 10, 30, 15).toISOString());
  });

  it("rejects empty, malformed and overflowing values", () => {
    for (const bad of ["", "not a date", "2026-11-13", "2026-02-31T10:00", "2026-11-13T10:30Z"]) {
      expect(localInputToIso(bad)).toBeNull();
    }
    expect(isoToLocalInput(null)).toBe("");
    expect(isoToLocalInput(undefined)).toBe("");
    expect(isoToLocalInput("garbage")).toBe("");
  });

  it("treats an ISO value without an offset as UTC, like the API", () => {
    expect(isoToLocalInput("2026-11-13T10:30:00")).toBe(isoToLocalInput("2026-11-13T10:30:00Z"));
    expect(isoToLocalInput("2026-11-13T10:30:00+00:00")).toBe(isoToLocalInput("2026-11-13T10:30:00Z"));
  });
});
