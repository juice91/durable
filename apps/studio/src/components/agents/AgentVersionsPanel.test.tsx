import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { AgentVersionsPanel } from "./AgentVersionsPanel";

describe("AgentVersionsPanel", () => {
  it("does not render when no versions", () => {
    const { container } = render(<AgentVersionsPanel versions={[]} />);
    expect(container).toBeEmpty();
  });

  it("renders versions list", () => {
    const versions = [
      { agent_id: "1", version: 1, spec_hash: "abc12345def67890", created_at: "2026-11-11T10:00:00Z" },
      { agent_id: "1", version: 2, spec_hash: "xyz98765fed43210", created_at: "2026-11-11T11:00:00Z" },
    ];
    const { container } = render(<AgentVersionsPanel versions={versions} />);

    expect(container.innerHTML).toContain("Versions");
    expect(container.innerHTML).toContain("Version");
    expect(container.innerHTML).toContain("Hash");
    expect(container.innerHTML).toContain("Created");
    expect(container.innerHTML).toContain("abc12345");
    expect(container.innerHTML).toContain("Showing 2 versions");
  });
});
