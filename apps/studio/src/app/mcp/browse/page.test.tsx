import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import McpCatalogPage from "./page";
import { routes } from "@/lib/routes";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/mcp/browse",
}));

describe("MCP Catalog page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ items: [], next_after: null }), { status: 200, headers: { "Content-Type": "application/json" } })));
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "dev-token");
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("is titled Catalog, with the ToolHive source selected and the search box focused", async () => {
    render(<McpCatalogPage />);
    expect(screen.getByRole("heading", { level: 1, name: "Catalog" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Catalog" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Servers" })).toHaveAttribute("href", routes.mcp());
    expect(screen.getByLabelText("Source")).toHaveValue("toolhive");
    await waitFor(() => expect(screen.getByRole("searchbox", { name: "Search the catalog" })).toHaveFocus());
  });

  it("offers Remote server by URL as a secondary button that opens the dialog", async () => {
    render(<McpCatalogPage />);
    fireEvent.click(screen.getByRole("button", { name: "Remote server by URL" }));
    expect(await screen.findByRole("dialog", { name: "Add MCP server" })).toBeInTheDocument();
  });
});
