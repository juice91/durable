import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "./Toaster";
import { CopyButton, COPIED_MS } from "./CopyButton";
import { CopyableText } from "./CopyableText";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";

vi.mock("./Toaster", () => ({ toast: vi.fn(), Toaster: () => null }));

const setClipboard = (writeText: unknown) => Object.defineProperty(navigator, "clipboard", { value: writeText ? { writeText } : undefined, configurable: true });
const setExec = (fn: unknown) => Object.defineProperty(document, "execCommand", { value: fn, configurable: true });

beforeEach(() => {
  vi.mocked(toast).mockClear();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  setClipboard(undefined);
  setExec(undefined);
});

describe("CopyButton", () => {
  it("has the name Copy, writes the text, and shows Copied for 1.5 s with no toast", async () => {
    vi.useFakeTimers();
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard(writeText);
    render(<CopyButton text="hello" />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy" })); });
    expect(writeText).toHaveBeenCalledWith("hello");
    expect(screen.getByRole("status")).toHaveTextContent("Copied");
    expect(toast).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(COPIED_MS + 10); });
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("reads a function's text at click time", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard(writeText);
    const text = vi.fn(() => "late");
    render(<CopyButton text={text} label="input" />);
    expect(text).not.toHaveBeenCalled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy input" })); });
    expect(writeText).toHaveBeenCalledWith("late");
  });

  it("falls back to a hidden textarea and execCommand when there is no clipboard API", async () => {
    const exec = vi.fn(() => true);
    setExec(exec);
    render(<CopyButton text="fallback" />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy" })); });
    expect(exec).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Copied");
    expect(toast).not.toHaveBeenCalled();
  });

  it("falls back when the clipboard write is refused", async () => {
    setClipboard(vi.fn().mockRejectedValue(new Error("denied")));
    const exec = vi.fn(() => true);
    setExec(exec);
    render(<CopyButton text="x" />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy" })); });
    expect(exec).toHaveBeenCalled();
  });

  it("toasts only when every path failed, and does not show Copied", async () => {
    setClipboard(vi.fn().mockRejectedValue(new Error("denied")));
    setExec(vi.fn(() => false));
    render(<CopyButton text="x" />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy" })); });
    expect(toast).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("does not let the click reach a parent", async () => {
    setClipboard(vi.fn().mockResolvedValue(undefined));
    const parent = vi.fn();
    render(<div onClick={parent}><CopyButton text="x" /></div>);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy" })); });
    expect(parent).not.toHaveBeenCalled();
  });
});

describe("CopyableText", () => {
  it("keeps its text selectable (not select-none) and copies what shows by default", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard(writeText);
    render(<CopyableText data-testid="c"><p>Hello <b>world</b></p></CopyableText>);
    expect(screen.getByTestId("c").className).toContain("select-text");
    expect(screen.getByTestId("c").className).not.toContain("select-none");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy" })); });
    expect(writeText).toHaveBeenCalledWith("Hello world");
  });

  it("copies copyText when the visible text differs", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard(writeText);
    render(<CopyableText copyText='{"a":1}' label="output"><pre>{"{ a: 1 }"}</pre></CopyableText>);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Copy output" })); });
    expect(writeText).toHaveBeenCalledWith('{"a":1}');
  });

  it("has a named copy button and no duplicate labels with distinct labels", async () => {
    const { container } = render(
      <div>
        <CopyableText label="input" copyText="a"><pre>a</pre></CopyableText>
        <CopyableText label="output" copyText="b"><pre>b</pre></CopyableText>
      </div>,
    );
    const names = screen.getAllByRole("button").map((b) => b.getAttribute("aria-label"));
    expect(names).toEqual(["Copy input", "Copy output"]);
    expect(new Set(names).size).toBe(names.length);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
