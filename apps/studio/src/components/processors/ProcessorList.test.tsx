import { render, screen, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { describe, expect, it, vi } from "vitest";
import { ProcessorList } from "./ProcessorList";
import type { ProcessorDeclaration } from "@/lib/processors";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));

function declaration(id: "pii_redact" | "moderation", phases: ProcessorDeclaration["phases"]): ProcessorDeclaration {
  return { id, title: id === "pii_redact" ? "PII redaction" : "Moderation", description: "d", phases, actions: ["block"], deterministic: id === "pii_redact", config_schema: {} };
}

const items = [declaration("pii_redact", ["input", "step", "stream", "output"]), declaration("moderation", ["input", "output"])];

function renderList() {
  return render(<ProcessorList items={items} usage={null} usageLoading usageError={null} outcomeCounts={null} />);
}

describe("ProcessorList phase columns", () => {
  it("has the columns Type, Description, Input, Step, Stream, Output, Used by, Outcomes (24 h) in that order", () => {
    renderList();
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent?.trim());
    expect(headers).toEqual(["Type", "Description", "Input", "Step", "Stream", "Output", "Used by", "Outcomes (24 h)"]);
  });

  it("marks each declared phase with a check and the others with a dash", () => {
    renderList();
    const rows = screen.getAllByTestId("processor-row");
    const marks = (row: HTMLElement) => ["input", "step", "stream", "output"].map((phase) => within(row).getByTestId(`phase-${phase}`).textContent?.trim());
    expect(marks(rows[0])).toEqual(["✓", "✓", "✓", "✓"]);
    expect(marks(rows[1])).toEqual(["✓", "—", "—", "✓"]);
  });
});
