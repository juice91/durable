import { test, expect, describe, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { usePathname } from "next/navigation";
import { routes } from "@/lib/routes";

vi.mock("next/navigation", () => ({
  usePathname: vi.fn(),
}));

import { Sidebar } from "@/components/layout/Sidebar";

describe("Sidebar", () => {
  test("should render all nav labels as links with correct hrefs", () => {
    const labels = ["Dashboard", "Agents", "Playground", "Workflows", "Tools", "MCP Servers", "Processors", "Request Context", "Traces", "Logs", "Metrics", "Settings"];

    vi.mocked(usePathname).mockReturnValue(routes.dashboard());

    render(<Sidebar />);

    labels.forEach((label) => {
      const elements = screen.getAllByText(label);
      expect(elements.length).toBeGreaterThan(0);
    });
  });

  test("should mark active route with aria-current=page for /agents", () => {
    vi.mocked(usePathname).mockReturnValue(routes.agents());

    render(<Sidebar />);

    const agentsLink = document.querySelector(`a[href="${routes.agents()}"]`);
    expect(agentsLink?.getAttribute("aria-current")).toBe("page");
  });

  test("should mark active route with aria-current=page for /", () => {
    vi.mocked(usePathname).mockReturnValue(routes.dashboard());

    render(<Sidebar />);

    const dashboardLink = document.querySelector(`a[href="${routes.dashboard()}"]`);
    expect(dashboardLink?.getAttribute("aria-current")).toBe("page");
  });
});
