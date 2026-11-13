import { describe, it, expect, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import type { AgentSpec } from "@/lib/api";
import { routes } from "@/lib/routes";
import { AgentsListUI } from "./AgentsList";

// SL.6: the list is a DataList whose rows link to the agent's chat (no onSelect callback any more).
const spec = (instructions: string): AgentSpec => ({ spec_version: 1, name: "x", instructions, model: { provider: "openai", name: "gpt-4.1" }, tools: [] });

beforeEach(() => window.localStorage.clear());

describe("AgentsListUI", () => {
  it("renders empty state when no agents", () => {
    render(<AgentsListUI agents={[]} />);
    expect(screen.getByText("No agents yet")).toBeInTheDocument();
    expect(screen.getByText("Create your first agent to get started.")).toBeInTheDocument();
  });

  it("renders agents list", () => {
    const agents = [
      { id: "1", name: "Agent One", latest_version: 1 },
      { id: "2", name: "Agent Two", latest_version: null },
    ];
    render(<AgentsListUI agents={agents} />);

    expect(screen.getByText("Agent One")).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("Agent Two")).toBeInTheDocument();
    expect(screen.getByText("Draft only")).toBeInTheDocument();
  });

  it("a row links to the agent's chat", () => {
    render(<AgentsListUI agents={[{ id: "abc", name: "Test", latest_version: null }]} />);
    expect(screen.getByRole("link", { name: "Test" })).toHaveAttribute("href", routes.agentChat("abc"));
  });

  it("ArrowDown from the search moves to the first row's link", () => {
    render(<AgentsListUI agents={[{ id: "abc", name: "Test", latest_version: null }]} />);
    fireEvent.keyDown(screen.getByRole("searchbox", { name: "Search agents" }), { key: "ArrowDown" });
    expect(screen.getByRole("link", { name: "Test" })).toHaveFocus();
  });

  it("the compact view shows cards that link to chat, and is remembered", () => {
    render(<AgentsListUI agents={[{ id: "abc", name: "Test", latest_version: null, draft_spec: spec("Answers questions") }]} />);
    fireEvent.click(screen.getByRole("radio", { name: "Compact" }));
    const card = screen.getByTestId("agent-card");
    expect(card).toHaveAttribute("href", routes.agentChat("abc"));
    expect(within(card).getByText("Answers questions")).toBeInTheDocument();
    expect(window.localStorage.getItem("studio.agents.view")).toBe("compact");
  });
});

describe("AgentsListUI search", () => {
  const agents = [
    { id: "1", name: "Support Bot", latest_version: 2 },
    { id: "2", name: "Billing Helper", latest_version: null, draft_spec: spec("Handles invoices") },
    { id: "3", name: "support triage", latest_version: 1 },
  ];

  it("filters by name or instructions, case-insensitively, and shows the count", async () => {
    render(<AgentsListUI agents={agents} />);
    const search = screen.getByRole("searchbox", { name: "Search agents" });
    expect(screen.getByTestId("agents-count")).toHaveTextContent("3 agents");
    fireEvent.change(search, { target: { value: "  SUPPORT " } });
    await waitFor(() => expect(screen.queryByText("Billing Helper")).not.toBeInTheDocument());
    expect(screen.getByText("Support Bot")).toBeInTheDocument();
    expect(screen.getByText("support triage")).toBeInTheDocument();
    expect(screen.getByTestId("agents-count")).toHaveTextContent("2 of 3 agents");
    fireEvent.change(search, { target: { value: "invoices" } });
    await waitFor(() => expect(screen.getByTestId("agents-count")).toHaveTextContent("1 of 3 agents"));
    expect(screen.getByText("Billing Helper")).toBeInTheDocument();
  });

  it("shows a no-match state", async () => {
    render(<AgentsListUI agents={agents} />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search agents" }), { target: { value: "zzz" } });
    // Shown in the table and announced by the list's live region.
    await waitFor(() => expect(screen.getByTestId("datalist-status")).toHaveTextContent("No agent name or instructions match this search."));
    expect(screen.getAllByText("No agent name or instructions match this search.")).toHaveLength(2);
    expect(screen.getByTestId("agents-count")).toHaveTextContent("0 of 3 agents");
  });
});
