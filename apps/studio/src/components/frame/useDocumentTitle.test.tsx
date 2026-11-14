import React from "react";
import { render, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useDocumentTitle } from "./useDocumentTitle";

function Title({ value }: { value: string | null }) {
  useDocumentTitle(value);
  return null;
}

describe("useDocumentTitle (browser check: Next's static title overwrote the page's)", () => {
  it("sets the title and puts it back when something else (Next's metadata) overwrites it", async () => {
    const view = render(<Title value="Agents · AI Platform Studio" />);
    expect(document.title).toBe("Agents · AI Platform Studio");
    document.title = "AI Platform Studio";
    await waitFor(() => expect(document.title).toBe("Agents · AI Platform Studio"));
    view.rerender(<Title value="Support bot · AI Platform Studio" />);
    expect(document.title).toBe("Support bot · AI Platform Studio");
    view.unmount();
    document.title = "Elsewhere";
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.title).toBe("Elsewhere");
  });

  it("with two titles mounted, the newest wins without a tug of war; unmounting it restores the other", async () => {
    const outer = render(<Title value="Workflow · AI Platform Studio" />);
    const inner = render(<Title value="Run · AI Platform Studio" />);
    expect(document.title).toBe("Run · AI Platform Studio");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.title).toBe("Run · AI Platform Studio");
    inner.unmount();
    expect(document.title).toBe("Workflow · AI Platform Studio");
    outer.unmount();
  });

  it("null leaves the title alone", () => {
    document.title = "Untouched";
    render(<Title value={null} />);
    expect(document.title).toBe("Untouched");
  });
});
