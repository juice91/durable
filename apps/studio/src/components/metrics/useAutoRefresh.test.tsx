import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useAutoRefresh } from "./useAutoRefresh";

const MIN = 60_000;
let visibility: DocumentVisibilityState = "visible";

function setVisibility(next: DocumentVisibilityState) {
  visibility = next;
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

describe("useAutoRefresh", () => {
  beforeEach(() => {
    visibility = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("ticks every interval while visible", () => {
    const { result } = renderHook(() => useAutoRefresh(true, 5 * MIN));
    expect(result.current).toBe(0);
    advance(5 * MIN - 1);
    expect(result.current).toBe(0);
    advance(1);
    expect(result.current).toBe(1);
    advance(10 * MIN);
    expect(result.current).toBe(3);
  });

  it("pauses while hidden and ticks once when visible again, then resumes the interval", () => {
    const { result } = renderHook(() => useAutoRefresh(true, 5 * MIN));
    advance(5 * MIN);
    expect(result.current).toBe(1);
    setVisibility("hidden");
    advance(60 * MIN);
    expect(result.current).toBe(1);
    setVisibility("visible");
    expect(result.current).toBe(2);
    advance(5 * MIN);
    expect(result.current).toBe(3);
  });

  it("does not start while mounted in a hidden tab", () => {
    visibility = "hidden";
    const { result } = renderHook(() => useAutoRefresh(true, 5 * MIN));
    advance(30 * MIN);
    expect(result.current).toBe(0);
    setVisibility("visible");
    expect(result.current).toBe(1);
  });

  it("never ticks when disabled, even on visibility changes", () => {
    const { result } = renderHook(() => useAutoRefresh(false, 5 * MIN));
    advance(30 * MIN);
    setVisibility("hidden");
    setVisibility("visible");
    expect(result.current).toBe(0);
  });

  it("stops on unmount", () => {
    const remove = vi.spyOn(document, "removeEventListener");
    const { unmount } = renderHook(() => useAutoRefresh(true, 5 * MIN));
    unmount();
    expect(remove).toHaveBeenCalledWith("visibilitychange", expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
  });
});
