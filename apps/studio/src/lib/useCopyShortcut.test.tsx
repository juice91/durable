import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useCopyShortcut } from "./useCopyShortcut";

const copy = vi.fn().mockResolvedValue(undefined);
Object.defineProperty(navigator, "clipboard", { value: { writeText: copy }, configurable: true });

function Probe() {
  useCopyShortcut();
  return (
    <div>
      <div data-copy-text="row text"><button type="button">in row</button></div>
      <button type="button">outside</button>
      <span>some words</span>
      <input aria-label="field" />
    </div>
  );
}

afterEach(() => { cleanup(); copy.mockClear(); window.getSelection()?.removeAllRanges(); });

describe("useCopyShortcut", () => {
  it("copies the focused row's text on Ctrl+C and Cmd+C", () => {
    render(<Probe />);
    screen.getByText("in row").focus();
    fireEvent.keyDown(document.activeElement as Element, { key: "c", ctrlKey: true });
    fireEvent.keyDown(document.activeElement as Element, { key: "c", metaKey: true });
    expect(copy).toHaveBeenCalledTimes(2);
    expect(copy).toHaveBeenCalledWith("row text");
  });

  it("does nothing outside a row, in a field, or with a selection", () => {
    render(<Probe />);
    screen.getByText("outside").focus();
    fireEvent.keyDown(document.activeElement as Element, { key: "c", ctrlKey: true });
    screen.getByLabelText("field").focus();
    fireEvent.keyDown(document.activeElement as Element, { key: "c", ctrlKey: true });
    screen.getByText("in row").focus();
    const range = document.createRange();
    range.selectNodeContents(screen.getByText("some words"));
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
    expect(window.getSelection()?.toString()).toBe("some words");
    fireEvent.keyDown(document.activeElement as Element, { key: "c", ctrlKey: true });
    expect(copy).not.toHaveBeenCalled();
  });
});
