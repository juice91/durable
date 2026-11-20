import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { StatusPill } from "./StatusPill";

afterEach(() => cleanup());

describe("StatusPill", () => {
  it("shows skipped as a grey Skipped badge, not a success or failure colour", () => {
    render(<StatusPill status="skipped" />);
    const pill = screen.getByTestId("status-pill");
    expect(pill).toHaveTextContent("Skipped");
    expect(pill).toHaveAttribute("data-status", "skipped");
    expect(pill.className).toContain("text-muted-foreground");
    expect(pill.className).not.toMatch(/success|destructive|warning/);
  });
});
