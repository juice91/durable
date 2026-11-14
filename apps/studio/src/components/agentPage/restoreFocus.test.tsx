import React, { useEffect } from "react";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RESTORE_FOCUS_SAFETY_MS, RESTORE_FOCUS_WINDOW_MS, useRestoreFocus, type RestoreFocus } from "./restoreFocus";

let api: RestoreFocus;
function Harness({ notice = true }: { notice?: boolean }) {
  const focus = useRestoreFocus();
  useEffect(() => {
    api = focus;
  });
  return (
    <>
      <button type="button">Restore v6</button>
      {notice ? (
        <p role="status" data-restore-notice="" tabIndex={-1}>
          Version 10 published, restored from v6
        </p>
      ) : null}
    </>
  );
}

const closeEvent = () => new Event("focusScope.autoFocusOnUnmount", { cancelable: true });

afterEach(() => vi.useRealTimers());

describe("useRestoreFocus (browser re-check: the closing Sheet dropped focus on <body> after the notice had it)", () => {
  it("prevents the Sheet's focus return for this flow and focuses the notice, even after the notice already took focus", () => {
    render(<Harness />);
    act(() => api.arm());
    act(() => api.settle(false));
    const notice = screen.getByRole("status");
    expect(notice).toHaveFocus();
    // 30 ms later the Sheet closes: its focus return must be prevented, and focus stays on the notice.
    const event = closeEvent();
    act(() => api.onSheetCloseAutoFocus(event));
    expect(event.defaultPrevented).toBe(true);
    expect(notice).toHaveFocus();
  });

  it("leaves the Sheet's focus return alone outside a restore, and after the window", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    render(<Harness />);
    const before = closeEvent();
    api.onSheetCloseAutoFocus(before);
    expect(before.defaultPrevented).toBe(false);
    act(() => api.arm());
    vi.setSystemTime(Date.now() + RESTORE_FOCUS_WINDOW_MS + 1);
    const after = closeEvent();
    api.onSheetCloseAutoFocus(after);
    expect(after.defaultPrevented).toBe(false);
  });

  it("waits while the Sheet is open, then focuses the notice", () => {
    render(<Harness />);
    act(() => api.arm());
    act(() => api.settle(true));
    expect(screen.getByRole("status")).not.toHaveFocus();
    act(() => api.settle(false));
    expect(screen.getByRole("status")).toHaveFocus();
  });

  it("puts focus back on the notice once if it falls to <body> within 500 ms", async () => {
    vi.useFakeTimers();
    render(<Harness />);
    act(() => api.arm());
    act(() => api.settle(false));
    const notice = screen.getByRole("status");
    act(() => notice.blur());
    expect(document.activeElement).toBe(document.body);
    await act(async () => {
      vi.advanceTimersByTime(1);
    });
    expect(notice).toHaveFocus();
    // Only once.
    act(() => notice.blur());
    await act(async () => {
      vi.advanceTimersByTime(RESTORE_FOCUS_SAFETY_MS);
    });
    expect(document.activeElement).toBe(document.body);
  });

  it("does not take focus back from where the user moved it", async () => {
    vi.useFakeTimers();
    render(<Harness />);
    act(() => api.arm());
    act(() => api.settle(false));
    const other = screen.getByRole("button", { name: "Restore v6" });
    act(() => other.focus());
    await act(async () => {
      vi.advanceTimersByTime(RESTORE_FOCUS_SAFETY_MS + 1);
    });
    expect(other).toHaveFocus();
  });
});
