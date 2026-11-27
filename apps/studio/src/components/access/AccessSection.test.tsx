import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AccessStatus } from "@/lib/agentGrants";

const mocks = vi.hoisted(() => ({ getAccess: vi.fn(), addGrant: vi.fn(), removeGrant: vi.fn() }));
vi.mock("@/lib/agentGrants", async (importOriginal) => ({ ...(await importOriginal<object>()), ...mocks }));

import { AccessSection } from "./AccessSection";

const status = (over: Partial<AccessStatus> = {}): AccessStatus => ({
  subject: { kind: "agent", id: "a1" },
  supported: true,
  enforced: true,
  can_manage: false,
  options: null,
  grants: [
    { kind: "mcp_server", id: "s1", name: "tickets", credentials: ["Authorization", "API_KEY"], used_by_latest: true, granted_by: "ann", granted_at: "2026-11-20T09:00:00Z" },
    { kind: "secret", id: "crm_token", name: "crm_token", credentials: [], used_by_latest: false },
  ],
  ...over,
});

beforeEach(() => {
  mocks.getAccess.mockReset();
  mocks.addGrant.mockReset().mockResolvedValue(undefined);
  mocks.removeGrant.mockReset().mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe("AccessSection", () => {
  it("lists the granted servers and secrets read-only for someone who is not an admin", async () => {
    mocks.getAccess.mockResolvedValue(status());
    render(<AccessSection kind="agent" id="a1" />);
    const server = await screen.findByTestId("access-grant-mcp_server-tickets");
    expect(server).toHaveTextContent("MCP server");
    expect(server).toHaveTextContent("This server uses credentials: Authorization, API_KEY");
    expect(server).toHaveTextContent("Granted by ann");
    expect(screen.getByTestId("access-grant-secret-crm_token")).toHaveTextContent("Granted, unused by the latest version");
    expect(screen.queryByRole("button", { name: /Remove/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull();
  });

  it("shows nothing when the policy engine keeps no grants or the section cannot be read", async () => {
    mocks.getAccess.mockResolvedValue(status({ supported: false, grants: [] }));
    const { container, rerender } = render(<AccessSection kind="agent" id="a1" />);
    await waitFor(() => expect(mocks.getAccess).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    mocks.getAccess.mockRejectedValue(new Error("down"));
    rerender(<AccessSection kind="agent" id="a2" />);
    await waitFor(() => expect(mocks.getAccess).toHaveBeenCalledTimes(2));
    expect(container).toBeEmptyDOMElement();
  });

  it("lets an admin remove a grant and reloads", async () => {
    mocks.getAccess.mockResolvedValue(status({ can_manage: true, options: { mcp_servers: [], secrets: [] } }));
    render(<AccessSection kind="workflow" id="w1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove crm_token" }));
    await waitFor(() => expect(mocks.removeGrant).toHaveBeenCalledWith("workflow", "w1", { kind: "secret", name: "crm_token" }));
    await waitFor(() => expect(mocks.getAccess).toHaveBeenCalledTimes(2));
  });

  it("offers an admin only what is not granted yet, and says so when the server refuses", async () => {
    mocks.getAccess.mockResolvedValue(status({ can_manage: true, options: { mcp_servers: [{ id: "s1", name: "tickets" }, { id: "s2", name: "wiki" }], secrets: ["crm_token", "other"] } }));
    const { ApiError } = await import("@/lib/api");
    mocks.addGrant.mockRejectedValue(new ApiError(403, "not allowed"));
    render(<AccessSection kind="agent" id="a1" />);
    const trigger = await screen.findByRole("combobox", { name: "Add an MCP server or secret" });
    fireEvent.click(trigger);
    expect(await screen.findByRole("option", { name: "MCP server · wiki" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "MCP server · tickets" })).toBeNull(); // already granted
    expect(screen.queryByRole("option", { name: "Secret · crm_token" })).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: "Secret · other" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mocks.addGrant).toHaveBeenCalledWith("agent", "a1", expect.objectContaining({ kind: "secret", name: "other" })));
    expect(await screen.findByTestId("access-error")).toHaveTextContent("not allowed");
  });

  it("says the grants are not enforced yet while the switch is off", async () => {
    mocks.getAccess.mockResolvedValue(status({ enforced: false }));
    render(<AccessSection kind="agent" id="a1" />);
    expect(await screen.findByText("Not enforced yet")).toBeInTheDocument();
  });
});
