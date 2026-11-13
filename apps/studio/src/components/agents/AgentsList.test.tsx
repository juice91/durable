import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import { AgentsListUI } from "./AgentsList";

describe("AgentsListUI", () => {
  it("renders empty state when no agents", () => {
    render(<AgentsListUI agents={[]} onSelect={vi.fn()} />);
    expect(screen.getByText("No agents yet")).toBeInTheDocument();
    expect(screen.getByText("Create your first agent to get started.")).toBeInTheDocument();
  });

  it("renders agents list", () => {
    const agents = [
      { id: "1", name: "Agent One", latest_version: 1 },
      { id: "2", name: "Agent Two", latest_version: null },
    ];
    const onSelect = vi.fn();
    render(<AgentsListUI agents={agents} onSelect={onSelect} />);

    expect(screen.getByText("Agent One")).toBeInTheDocument();
    expect(screen.getByText("Version 1")).toBeInTheDocument();
    expect(screen.getByText("Agent Two")).toBeInTheDocument();
    expect(screen.getByText("Draft")).toBeInTheDocument();
  });

  it("calls onSelect when clicking an agent", () => {
    const agents = [{ id: "abc", name: "Test", latest_version: null }];
    const onSelect = vi.fn();
    render(<AgentsListUI agents={agents} onSelect={onSelect} />);

    const card = screen.getByText("Test");
    fireEvent.click(card);
    expect(onSelect).toHaveBeenCalledWith("abc");
  });

  it("calls onSelect on Enter key press", () => {
    const agents = [{ id: "abc", name: "Test", latest_version: null }];
    const onSelect = vi.fn();
    render(<AgentsListUI agents={agents} onSelect={onSelect} />);

    const card = screen.getByText("Test");
    fireEvent.keyDown(card, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith("abc");
  });

  it("calls onSelect on Space key press", () => {
    const agents = [{ id: "abc", name: "Test", latest_version: null }];
    const onSelect = vi.fn();
    render(<AgentsListUI agents={agents} onSelect={onSelect} />);

    const card = screen.getByText("Test");
    fireEvent.keyDown(card, { key: " " });
    expect(onSelect).toHaveBeenCalledWith("abc");
  });
});

describe("AgentsListUI search", () => {
  const agents = [
    { id: "1", name: "Support Bot", latest_version: 2 },
    { id: "2", name: "Billing Helper", latest_version: null },
    { id: "3", name: "support triage", latest_version: 1 },
  ];

  it("filters by name, case-insensitively, and announces the count", () => {
    render(<AgentsListUI agents={agents} onSelect={vi.fn()} />);
    const search = screen.getByRole("searchbox", { name: "Search agents" });
    expect(screen.getByRole("status")).toHaveTextContent("3 agents");
    fireEvent.change(search, { target: { value: "  SUPPORT " } });
    expect(screen.getByText("Support Bot")).toBeInTheDocument();
    expect(screen.getByText("support triage")).toBeInTheDocument();
    expect(screen.queryByText("Billing Helper")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("2 of 3 agents");
  });

  it("shows a no-match state", () => {
    render(<AgentsListUI agents={agents} onSelect={vi.fn()} />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search agents" }), { target: { value: "zzz" } });
    expect(screen.getByText("No matching agents")).toBeInTheDocument();
    expect(screen.getByText('No agent name contains "zzz".')).toBeInTheDocument();
  });
});
