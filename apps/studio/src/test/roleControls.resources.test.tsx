import React from "react";
import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ search: "", params: {} as Record<string, string> }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useParams: () => nav.params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));

import KnowledgeBasePage from "@/app/knowledge/[id]/page";
import KnowledgePage from "@/app/knowledge/page";
import McpServerPage from "@/app/mcp/[id]/page";
import McpCatalogPage from "@/app/mcp/browse/page";
import McpPage from "@/app/mcp/page";
import { resetCapabilitiesForTests } from "@/lib/iam";
import { renderAs, settle, type Routes } from "./roleHarness";
import type { TestRole } from "./iamFixtures";

const T = "2026-11-14T10:00:00Z";
const button = (name: string | RegExp) => screen.queryByRole("button", { name });

vi.unmock("@/lib/permissions");

afterEach(() => {
  cleanup();
  resetCapabilitiesForTests();
  vi.unstubAllGlobals();
  nav.search = "";
  nav.params = {};
});

const KB = { id: "k1", tenant_id: "t", name: "Handbook", description: null, embedding_model: "m", dimensions: 8, chunking: {}, active_generation: 1, last_generation: 1, pending_generation: null, pending_embedding_model: null, pending_dimensions: null, status: "ready", error: null, created_at: T, updated_at: T, document_count: 0, chunk_count: 0, ingest_tokens: 0, ingest_cost_usd: "0", ingest_requests: 0 };

describe("Knowledge", () => {
  const list: Routes = [[/GET .*\/knowledge-bases$/, { items: [KB] }], [/GET .*\/knowledge-bases\/k1$/, KB]];
  it("offers a viewer no New knowledge base", async () => {
    renderAs("viewer", <KnowledgePage />, list);
    await screen.findByText("Handbook");
    await settle();
    expect(screen.queryByTestId("knowledge-new")).toBeNull();
  });
  it.each(["editor", "admin"] as TestRole[])("offers an %s New knowledge base", async (role) => {
    renderAs(role, <KnowledgePage />, list);
    expect(await screen.findByTestId("knowledge-new")).toBeTruthy();
  });

  const detail: Routes = [
    [/GET .*\/knowledge-bases\/k1$/, KB],
    [/GET .*\/knowledge-bases\/k1\/documents/, { items: [], next_after: null }],
    [/GET .*\/knowledge-bases\/k1\/sources$/, { items: [{ id: "s1", name: "Docs repo", provider: "github", enabled: true, connection_id: "c1", config: {}, last_successful_sync_at: null, last_checkpoint_summary: { segments: 0, has_cursor: false } }] }],
    [/GET .*\/knowledge-bases$/, { items: [KB] }],
    [/GET .*\/kb-sources\/adapters/, { items: [] }],
    [/GET .*\/settings\/connections/, { items: [] }],
  ];
  const open = (role: TestRole, tab?: string) => {
    nav.params = { id: "k1" };
    nav.search = tab ? `tab=${tab}` : "";
    renderAs(role, <KnowledgeBasePage />, detail);
  };
  it("offers a viewer no upload, reindex or delete", async () => {
    open("viewer");
    await screen.findByTestId("knowledge-summary");
    await settle();
    ["knowledge-upload", "knowledge-reindex", "knowledge-delete"].forEach((id) => expect(screen.queryByTestId(id), id).toBeNull());
    expect(button("Upload documents")).toBeNull();
  });
  it("offers an editor upload and reindex, not delete", async () => {
    open("editor");
    expect(await screen.findByTestId("knowledge-upload")).toBeTruthy();
    expect(screen.getByTestId("knowledge-reindex")).toBeTruthy();
    expect(screen.queryByTestId("knowledge-delete")).toBeNull();
  });
  it("offers an admin delete too", async () => {
    open("admin");
    expect(await screen.findByTestId("knowledge-delete")).toBeTruthy();
  });
  it("offers a viewer no source changes or sync", async () => {
    open("viewer", "sources");
    await screen.findByText("Docs repo");
    await settle();
    ["Add source", "Run sync for Docs repo", "Disable Docs repo", "Publish the sync workflow"].forEach((name) => expect(button(name), name).toBeNull());
    expect(button("Show syncs for Docs repo")).toBeTruthy();
  });
  it("offers an editor the source controls", async () => {
    open("editor", "sources");
    for (const name of ["Add source", "Run sync for Docs repo", "Disable Docs repo"]) expect(await screen.findByRole("button", { name })).toBeTruthy();
  });
  it("offers a viewer no search tester run", async () => {
    open("viewer", "search");
    await screen.findByTestId("knowledge-search-tab");
    await settle();
    expect(screen.queryByTestId("knowledge-search-submit")).toBeNull();
  });
  it("offers an editor the search tester", async () => {
    open("editor", "search");
    expect(await screen.findByTestId("knowledge-search-submit")).toBeTruthy();
  });
});

const SERVER = { id: "m1", name: "Docs", url: "https://docs.example.test/mcp", transport: "streamable_http", enabled: true, header_names: [], created_at: T, updated_at: T, last_checked_at: null, last_status: null, last_error_code: null };

describe("MCP servers", () => {
  const routes: Routes = [[/GET .*\/mcp-servers\/m1$/, SERVER], [/GET .*\/mcp-servers$/, [SERVER]]];
  const row = ["Test connection", "Disable", "Edit", "Delete"];
  it.each(["viewer", "editor"] as TestRole[])("offers a %s no add and no row controls", async (role) => {
    renderAs(role, <McpPage />, routes);
    await screen.findByText("Docs");
    await settle();
    expect(screen.queryByTestId("mcp-add-server")).toBeNull();
    row.forEach((name) => expect(button(name), name).toBeNull());
  });
  it("offers an admin all of them", async () => {
    renderAs("admin", <McpPage />, routes);
    expect(await screen.findByTestId("mcp-add-server")).toBeTruthy();
    for (const name of row) expect(await screen.findByRole("button", { name })).toBeTruthy();
  });
  it.each(["viewer", "editor"] as TestRole[])("offers a %s no server changes on its page", async (role) => {
    nav.params = { id: "m1" };
    renderAs(role, <McpServerPage />, routes);
    await screen.findByTestId("mcp-connection");
    await settle();
    expect(screen.queryByTestId("mcp-delete")).toBeNull();
    ["Test connection", "Disable", "Edit"].forEach((name) => expect(button(name), name).toBeNull());
  });
  it("offers an admin the server page controls", async () => {
    nav.params = { id: "m1" };
    renderAs("admin", <McpServerPage />, routes);
    expect(await screen.findByTestId("mcp-delete")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Edit" })).toBeTruthy();
    await userEvent.setup().click(screen.getByTestId("mcp-tab-run"));
  });
  it("offers only an admin a remote server from the catalog", async () => {
    const catalog: Routes = [[/GET .*\/mcp-catalog/, { items: [], next_cursor: null, sources: [] }]];
    renderAs("viewer", <McpCatalogPage />, catalog);
    await settle();
    expect(button("Remote server by URL")).toBeNull();
    cleanup();
    resetCapabilitiesForTests();
    renderAs("admin", <McpCatalogPage />, catalog);
    expect(await screen.findByRole("button", { name: "Remote server by URL" })).toBeTruthy();
  });
});
