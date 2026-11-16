import { fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { describe, expect, it } from "vitest";
import { ProcessorTestConsole } from "./ProcessorTestConsole";
import type { ProcessorCatalog } from "@/lib/processors";

const catalog: ProcessorCatalog = {
  items: [
    { id: "pii_redact", title: "PII redaction", description: "", phases: ["input", "step", "stream", "output"], actions: ["redact"], deterministic: true, config_schema: { type: "object", properties: {} } },
    { id: "max_length", title: "Maximum length", description: "", phases: ["input", "output"], actions: ["block"], deterministic: true, config_schema: { type: "object", properties: {} } },
  ],
  limits: { max_processors_per_phase: 8, max_regex_patterns: 20, max_regex_pattern_length: 200, max_test_sample_bytes: 32768 },
  moderation_categories: [],
};

describe("ProcessorTestConsole phase select", () => {
  it("lists the four phases, input first", () => {
    render(<ProcessorTestConsole catalog={catalog} />);
    const options = within(screen.getByLabelText("Phase")).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["Input", "Step", "Stream", "Output"]);
  });

  it("says the test runs the same checks in every phase", () => {
    render(<ProcessorTestConsole catalog={catalog} />);
    expect(screen.getByText(/The test runs the same checks in every phase/)).toBeInTheDocument();
  });

  it("offers only the types that run in the chosen phase", () => {
    render(<ProcessorTestConsole catalog={catalog} />);
    const types = () => within(screen.getByLabelText("Processor 1")).getAllByRole("option").map((o) => o.textContent);
    expect(types()).toEqual(["PII redaction", "Maximum length"]);
    fireEvent.change(screen.getByLabelText("Phase"), { target: { value: "stream" } });
    expect(types()).toEqual(["PII redaction"]);
  });
});
