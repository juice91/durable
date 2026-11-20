import { fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { describe, expect, it, vi } from "vitest";
import { ProcessorsEditor } from "./ProcessorsEditor";
import { modelsFixture, processorCatalogFixture } from "./specSections.fixtures";
import type { EditableProcessor } from "./specSections";
import { optionsOf } from "@/test/select";

const pii = (clientId: string): EditableProcessor => ({ type: "pii_redact", config: { kinds: ["email"], action: "redact" }, clientId });

function renderEditor(overrides: Partial<React.ComponentProps<typeof ProcessorsEditor>> = {}) {
  const props: React.ComponentProps<typeof ProcessorsEditor> = {
    inputProcessors: [],
    stepProcessors: [],
    streamProcessors: [],
    outputProcessors: [],
    onInputChange: vi.fn(),
    onStepChange: vi.fn(),
    onStreamChange: vi.fn(),
    onOutputChange: vi.fn(),
    catalog: processorCatalogFixture,
    catalogError: null,
    models: modelsFixture,
    modelsError: null,
    ...overrides,
  };
  render(<ProcessorsEditor {...props} />);
  return props;
}

describe("ProcessorsEditor phases", () => {
  it("shows four lists with their titles, in order", () => {
    renderEditor();
    const titles = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(titles).toEqual(["Input processors", "Step processors", "Stream processors", "Output processors"]);
  });

  it("explains streaming per phase", () => {
    renderEditor();
    expect(within(screen.getByTestId("processors-stream")).getByText(/Live streaming stays on/)).toBeInTheDocument();
    expect(within(screen.getByTestId("processors-output")).getByText(/Without a stream processor, any output processor holds the answer/)).toBeInTheDocument();
  });

  it("offers moderation under Input and Output only", () => {
    renderEditor();
    const options = (label: string) => optionsOf(screen.getByLabelText(label)).map((o) => o.textContent);
    expect(options("Input processor type")).toContain("Moderation");
    expect(options("Output processor type")).toContain("Moderation");
    expect(options("Step processor type")).not.toContain("Moderation");
    expect(options("Stream processor type")).not.toContain("Moderation");
    expect(options("Step processor type")).toContain("PII redaction");
  });

  it("adds to the step and stream lists through their own callbacks", () => {
    const props = renderEditor({ streamProcessors: [pii("p9")] });
    fireEvent.change(screen.getByLabelText("Step processor type"), { target: { value: "pii_redact" } });
    fireEvent.click(screen.getByRole("button", { name: "Add step processor" }));
    expect(props.onStepChange).toHaveBeenCalledTimes(1);
    expect(vi.mocked(props.onStepChange).mock.calls[0][0]).toMatchObject([{ type: "pii_redact" }]);
    expect(props.onStreamChange).not.toHaveBeenCalled();
    expect(within(screen.getByTestId("processors-stream")).getByText(/1\. PII redaction/)).toBeInTheDocument();
  });
});
