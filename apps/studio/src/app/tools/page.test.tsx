import { fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ToolsPage from "./page";
import { listTools } from "@/lib/tools";

vi.mock("@/lib/tools", () => ({ listTools: vi.fn() }));

const tools = [
  {
    id: "tickets.create",
    title: "Create ticket",
    description: "Creates a support ticket",
    side_effect: true,
    external_idempotent: false,
    input_schema: {
      type: "object" as const,
      required: ["subject"],
      properties: { subject: { type: "string" as const, title: "Subject" } },
    },
    allowed: true,
  },
  {
    id: "clock.now",
    title: "Current time",
    description: "Returns the current time",
    side_effect: false,
    external_idempotent: true,
    input_schema: { type: "object" as const, properties: {} },
    allowed: false,
  },
];

describe("ToolsPage", () => {
  beforeEach(() => vi.mocked(listTools).mockResolvedValue(tools));

  it("searches the catalog and displays policy badges", async () => {
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });

    expect(screen.getByText("Side effect")).toBeInTheDocument();
    expect(screen.getByText("Disallowed")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search tools"), { target: { value: "clock" } });
    const catalog = screen.getByLabelText("Tool catalog");
    expect(within(catalog).queryByText("Create ticket")).not.toBeInTheDocument();
    expect(within(catalog).getByText("Current time")).toBeInTheDocument();
  });

  it("keeps form and raw JSON modes in sync", async () => {
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });

    fireEvent.change(screen.getByLabelText(/Subject \*/), { target: { value: "Printer down" } });
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    expect((screen.getByLabelText("Tool input JSON") as HTMLTextAreaElement).value).toContain("Printer down");

    fireEvent.change(screen.getByLabelText("Tool input JSON"), {
      target: { value: JSON.stringify({ subject: "Network down" }) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Form" }));
    expect(screen.getByLabelText(/Subject \*/)).toHaveValue("Network down");
  });

  it("shows invalid JSON inline and leaves running disabled", async () => {
    render(<ToolsPage />);
    await screen.findByRole("heading", { name: "Create ticket" });
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    fireEvent.change(screen.getByLabelText("Tool input JSON"), { target: { value: "{" } });

    expect(screen.getByText("Enter valid JSON")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    expect(screen.getByText("Running tools arrives with tool test runs")).toBeInTheDocument();
  });
});
