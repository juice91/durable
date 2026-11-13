import { act, render, screen } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { describe, expect, it, vi } from "vitest";
import { ConfirmDialog } from "./ConfirmDialog";

function dialogHost(name: string) {
  return screen.getByRole("dialog", { name }).closest("[data-confirm-dialog-root]") as HTMLElement;
}

describe("ConfirmDialog", () => {
  it("renders the error inside the dialog as an alert", () => {
    render(<ConfirmDialog title="Delete x?" confirmLabel="Delete" error="It failed" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole("dialog")).toContainElement(screen.getByRole("alert"));
    expect(screen.getByRole("alert")).toHaveTextContent("It failed");
  });

  it("keeps the page inert until the last of two stacked dialogs closes, and only the top one is live", () => {
    const page = render(<button type="button">Page button</button>);
    const pageRoot = page.container;
    const first = render(<ConfirmDialog title="First" confirmLabel="OK" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(pageRoot).toHaveAttribute("inert");

    const second = render(<ConfirmDialog title="Second" confirmLabel="OK" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(dialogHost("Second")).not.toHaveAttribute("inert");
    expect(document.querySelectorAll("[data-confirm-dialog-root]")[0]).toHaveAttribute("inert");

    second.unmount();
    expect(pageRoot).toHaveAttribute("inert");
    expect(dialogHost("First")).not.toHaveAttribute("inert");

    first.unmount();
    expect(pageRoot).not.toHaveAttribute("inert");
  });

  it("makes body children added while open inert, and leaves pre-existing inert elements alone", async () => {
    const preInert = document.createElement("div");
    preInert.setAttribute("inert", "");
    document.body.appendChild(preInert);
    const dialog = render(<ConfirmDialog title="Open" confirmLabel="OK" onConfirm={vi.fn()} onCancel={vi.fn()} />);

    const late = document.createElement("div");
    await act(async () => {
      document.body.appendChild(late);
      await Promise.resolve();
    });
    expect(late).toHaveAttribute("inert");

    dialog.unmount();
    expect(late).not.toHaveAttribute("inert");
    expect(preInert).toHaveAttribute("inert");
    late.remove();
    preInert.remove();
  });
});
