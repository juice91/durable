import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { IamCapabilities } from "@/lib/iam";
import { visibleTabs } from "@/components/agentPage/AgentLayout";
import { knowledgeTabs } from "@/components/knowledge/knowledgeUi";
import { workflowTabs } from "@/components/workflows/WorkflowHeader";
import { agentTabOf, routes } from "@/lib/routes";
import { parseKnowledgeTab } from "@/lib/routes/knowledge";
import { parseWorkflowTab } from "@/lib/routes/workflows";

const mocks = vi.hoisted(() => ({ loadCapabilities: vi.fn(), getResourceAccess: vi.fn(), explainAccess: vi.fn(), listSecrets: vi.fn(), getServer: vi.fn() }));
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "srv-1" }), useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/iam", async (importOriginal) => ({ ...(await importOriginal<object>()), loadCapabilities: mocks.loadCapabilities }));
vi.mock("@/lib/access", async (importOriginal) => ({ ...(await importOriginal<object>()), getResourceAccess: mocks.getResourceAccess, explainAccess: mocks.explainAccess }));
vi.mock("@/lib/secrets", async (importOriginal) => ({ ...(await importOriginal<object>()), listSecrets: mocks.listSecrets }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, get: mocks.getServer, list: vi.fn().mockResolvedValue([]) } };
});

import McpServerDetailPage from "@/app/mcp/[id]/page";
import { SecretsSection } from "@/components/settings/SecretsSection";

const caps = (features: string[]): IamCapabilities => ({
  mode: "production", identity: [], membership: null, actions: [],
  policy: { name: features.length ? "openfga" : "openaccess", transitional: features.length === 0, features },
  me: { subject: "zitadel:me", email: null, tenant_id: "t", kind: "user", roles: ["admin"], provider: "zitadel" },
});

beforeEach(() => {
  Object.values(mocks).forEach((m) => m.mockReset());
  mocks.getServer.mockResolvedValue({ id: "srv-1", name: "docs", url: "https://mcp.example.test/mcp", transport: "streamable_http", enabled: true, header_names: [], created_at: null, updated_at: null, last_checked_at: null, last_status: null, last_error_code: null });
  mocks.listSecrets.mockResolvedValue([{ name: "crm_token", used_by: [] }]);
  mocks.getResourceAccess.mockResolvedValue({ resource: { kind: "secret", id: "crm_token" }, supported: true, can_manage: false, relations: [{ relation: "owner", roles: [] }], grants: [], used_by: [] });
  mocks.explainAccess.mockResolvedValue({ supported: true, kind: "secret", id: null, roles: ["viewer"], decisions: [{ action: "create", allowed: false, reason: "no_permission", needed: { roles: ["admin"], relations: [] } }] });
});
afterEach(() => cleanup());

describe("the Access tab of each page", () => {
  it("is offered on agents, knowledge bases and workflows only when the engine keeps grants", () => {
    expect(visibleTabs(false, "chat", true).map((t) => t.id)).toEqual(["chat", "traces", "editor", "tests", "access"]);
    expect(visibleTabs(false, "chat").map((t) => t.id)).not.toContain("access");
    expect(visibleTabs(false, "access").map((t) => t.id)).toContain("access"); // opened directly: the page says it is off
    expect(knowledgeTabs("kb", "documents", true).map((t) => t.label)).toContain("Access");
    expect(knowledgeTabs("kb", "documents").map((t) => t.label)).not.toContain("Access");
    expect(workflowTabs("w1", "graph", 0, true).map((t) => t.testId)).toContain("workflow-tab-access");
    expect(workflowTabs("w1", "graph", 0).map((t) => t.testId)).not.toContain("workflow-tab-access");
  });

  it("has its own URL", () => {
    expect(routes.agentTab("a 1", "access")).toBe("/agents/a%201/access");
    expect(agentTabOf("/agents/a1/access")).toBe("access");
    expect(routes.workflow("w1", { tab: "access" })).toBe("/workflows/w1?tab=access");
    expect(routes.knowledgeBase("k1", { tab: "access" })).toBe("/knowledge/k1?tab=access");
    expect(parseWorkflowTab("access")).toBe("access");
    expect(parseKnowledgeTab("access")).toBe("access");
  });

  it("is on the MCP server page under openfga and not under openaccess", async () => {
    mocks.loadCapabilities.mockResolvedValue(caps(["grants", "explain"]));
    const first = render(<McpServerDetailPage />);
    expect(await screen.findByTestId("mcp-tab-access")).toHaveTextContent("Access");
    first.unmount();
    mocks.loadCapabilities.mockResolvedValue(caps([]));
    render(<McpServerDetailPage />);
    await screen.findByTestId("mcp-tab-overview");
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId("mcp-tab-access")).toBeNull();
  });

  it("opens a secret's access from its row, and says why a new secret cannot be added", async () => {
    mocks.loadCapabilities.mockResolvedValue(caps(["grants", "explain"]));
    render(<SecretsSection />);
    expect(await screen.findByTestId("why-not")).toHaveTextContent("Your role (viewer) can't create a secret. Ask an admin for the admin role.");
    fireEvent.click(await screen.findByRole("button", { name: "Access to crm_token" }));
    expect(await screen.findByTestId("access-read-only")).toBeInTheDocument();
    expect(mocks.getResourceAccess).toHaveBeenCalledWith("secret", "crm_token", expect.anything());
  });

  it("adds nothing to the secrets list under openaccess", async () => {
    mocks.loadCapabilities.mockResolvedValue(caps([]));
    render(<SecretsSection />);
    await screen.findByTestId("secrets-list");
    await waitFor(() => expect(mocks.loadCapabilities).toHaveBeenCalled());
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByRole("button", { name: /Access to/ })).toBeNull();
    expect(screen.queryByTestId("why-not")).toBeNull();
    expect(mocks.explainAccess).not.toHaveBeenCalled();
  });
});
