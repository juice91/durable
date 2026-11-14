import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import React, { createRef } from "react";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: vi.fn(() => "/agents/a1/chat"),
  useRouter: vi.fn(() => ({ push, replace: vi.fn(), back: vi.fn() })),
  useSearchParams: vi.fn(() => new URLSearchParams()),
}));

import { documentTitleFor, PageHeader } from "./PageHeader";

beforeEach(() => {
  push.mockReset();
  document.title = "";
});

afterEach(() => {
  document.title = "";
});

describe("PageHeader", () => {
  test("the last crumb is the visible h1; ancestors are links in a Breadcrumb nav", () => {
    render(<PageHeader crumbs={[{ label: "Agents", href: routes.agents() }, { label: "Support bot" }]} />);
    const nav = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(nav).getByRole("link", { name: "Agents" })).toHaveAttribute("href", routes.agents());
    const heading = within(nav).getByRole("heading", { level: 1, name: "Support bot" });
    expect(heading).toBeVisible();
    expect(heading.closest("li")).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  test("a single crumb is just the h1", () => {
    render(<PageHeader crumbs={[{ label: "Traces" }]} />);
    expect(screen.getByRole("heading", { level: 1, name: "Traces" })).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  test("an ancestor without href is plain text", () => {
    render(<PageHeader crumbs={[{ label: "Runs" }, { label: "Run r1" }]} />);
    expect(screen.getByText("Runs").tagName).toBe("SPAN");
    expect(screen.queryByRole("link")).toBeNull();
  });

  test("sets document.title from the last crumb, or from documentTitle", () => {
    const { rerender } = render(<PageHeader crumbs={[{ label: "Metrics" }]} />);
    expect(document.title).toBe("Metrics · AI Platform Studio");
    rerender(<PageHeader crumbs={[{ label: <span>Custom</span> }]} documentTitle="Run abc" />);
    expect(document.title).toBe("Run abc · AI Platform Studio");
    expect(documentTitleFor("")).toBe("AI Platform Studio");
    expect(documentTitleFor(null)).toBe("AI Platform Studio");
  });

  test("renders actions, route tabs and the action row", () => {
    render(
      <PageHeader
        crumbs={[{ label: "Agents", href: routes.agents() }, { label: "Support bot" }]}
        actions={<button type="button" data-testid="primary-action">New</button>}
        tabs={[
          { href: "/agents/a1/chat", label: "Chat" },
          { href: "/agents/a1/editor", label: "Editor" },
        ]}
        actionRow={<input aria-label="Search agents" />}
      />,
    );
    const header = screen.getByTestId("page-header");
    expect(within(header).getByTestId("primary-action")).toBeInTheDocument();
    const tabs = within(header).getByRole("navigation", { name: "Support bot sections" });
    expect(within(tabs).getByRole("link", { name: "Chat" })).toHaveAttribute("aria-current", "page");
    expect(within(tabs).getByRole("link", { name: "Editor" })).not.toHaveAttribute("aria-current");
    expect(within(header).getByRole("textbox", { name: "Search agents" })).toBeInTheDocument();
  });

  test("passes a ref and props to the h1 (pages that focus their heading)", () => {
    const ref = createRef<HTMLHeadingElement>();
    render(<PageHeader crumbs={[{ label: "MCP Servers", testId: "mcp-title" }]} titleRef={ref} titleProps={{ tabIndex: -1 }} />);
    const heading = screen.getByRole("heading", { level: 1, name: "MCP Servers" });
    expect(ref.current).toBe(heading);
    expect(heading).toHaveAttribute("tabindex", "-1");
    expect(heading).toHaveAttribute("data-testid", "mcp-title");
  });

  test("an entity switcher lists entities and navigates to the chosen one", async () => {
    render(
      <PageHeader
        crumbs={[
          { label: "Agents", href: routes.agents() },
          {
            label: "Support bot",
            switcher: {
              label: "Switch agent",
              value: "a1",
              options: [
                { value: "a1", label: "Support bot" },
                { value: "a2", label: "Billing bot" },
              ],
              hrefFor: (id) => routes.agentEditor(id),
              testId: "agent-switcher",
            },
          },
        ]}
      />,
    );
    // The switcher has its own name; the heading keeps the crumb text.
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^Support bot$/);
    const trigger = screen.getByRole("combobox", { name: "Switch agent" });
    expect(trigger).toHaveAttribute("data-testid", "agent-switcher");
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("option", { name: /Billing bot/ }));
    expect(push).toHaveBeenCalledWith(routes.agentEditor("a2"));
  });

  test("choosing the current entity does not navigate", async () => {
    const onValueChange = vi.fn();
    render(
      <PageHeader
        crumbs={[
          {
            label: "Support bot",
            switcher: { label: "Switch agent", value: "a1", options: [{ value: "a1", label: "Support bot" }], hrefFor: (id) => routes.agentEditor(id), onValueChange },
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("combobox", { name: "Switch agent" }));
    fireEvent.click(await screen.findByRole("option", { name: /Support bot/ }));
    expect(onValueChange).toHaveBeenCalledWith("a1");
    expect(push).not.toHaveBeenCalled();
  });

  test("has no axe violations", async () => {
    const { container } = render(
      <PageHeader crumbs={[{ label: "Workflows", href: routes.workflows() }, { label: "Triage" }]} actions={<button type="button">Run</button>} />,
    );
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
