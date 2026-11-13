import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import AgentDetailPage from "./page";
import { api, ApiError } from "@/lib/api";
import { useRouter, useParams } from "next/navigation";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  useParams: () => ({ id: "a1" }),
  usePathname: () => "/agents/a1",
}));

vi.mock("@/lib/api", () => ({
  api: {
    agents: { get: vi.fn(), update: vi.fn() },
    versions: { list: vi.fn(), create: vi.fn() },
  },
  ApiError: class ApiError extends Error {
    constructor(public status: number, message: string) {
      super(message);
      this.name = "ApiError";
    }
  },
}));

describe("AgentDetailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("Publish on 409 shows server detail", async () => {
    const mockAgent = {
      id: "a1",
      name: "Test Agent",
      draft_spec: {
        spec_version: 1,
        name: "Test Agent",
        instructions: "Test",
        model: { provider: "openai", name: "gpt-4" },
        tools: [],
      },
      latest_version: 1,
    };
    (api.agents.get as any).mockResolvedValueOnce(mockAgent);
    (api.versions.list as any).mockResolvedValueOnce([]);
    (api.versions.create as any).mockRejectedValueOnce(new ApiError(409, "Nothing changed"));

    render(<AgentDetailPage />);

    await waitFor(() => {
      expect(api.agents.get).toHaveBeenCalledWith("a1");
    });

    const publishButton = screen.getByText("Publish");
    expect(publishButton).toBeInTheDocument();
    publishButton.click();

    await waitFor(() => {
      expect(api.versions.create).toHaveBeenCalledWith("a1");
      expect(screen.getByText(/nothing changed/i)).toBeInTheDocument();
    });
  });

  it("Publish 422 for an MCP server shows the server message", async () => {
    (api.agents.get as any).mockResolvedValueOnce({
      id: "a1",
      name: "Test Agent",
      draft_spec: {
        spec_version: 1,
        name: "Test Agent",
        instructions: "Test",
        model: { provider: "openai", name: "gpt-4" },
        tools: [],
        mcp_tools: [{ server: "x", tool: "search" }],
      },
      latest_version: 1,
    });
    (api.versions.list as any).mockResolvedValueOnce([]);
    (api.versions.create as any).mockRejectedValueOnce(new ApiError(422, "MCP server 'x' is disabled"));

    render(<AgentDetailPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Publish" }));

    expect(await screen.findByTestId("agent-save-error")).toHaveTextContent("MCP server 'x' is disabled");
  });

  it("shows error screen only on initial load failure", async () => {
    (api.agents.get as any).mockRejectedValueOnce(new ApiError(404, "Not found"));

    render(<AgentDetailPage />);

    await waitFor(() => {
      expect(screen.getByText(/agent not found/i)).toBeInTheDocument();
    });
  });

  it("Save draft sends PATCH with wrapped spec", async () => {
    const mockAgent = {
      id: "a1",
      name: "Original Agent",
      draft_spec: {
        spec_version: 1,
        name: "Original Agent",
        instructions: "Test",
        model: { provider: "openai", name: "gpt-4" },
        tools: [],
      },
      latest_version: 1,
    };

    (api.agents.get as any).mockResolvedValueOnce(mockAgent);
    (api.versions.list as any).mockResolvedValueOnce([]);
    (api.agents.update as any).mockResolvedValueOnce({ ...mockAgent, name: "Updated Agent" });

    render(<AgentDetailPage />);

    await waitFor(() => {
      expect(api.agents.get).toHaveBeenCalledWith("a1");
    });

    const nameInput = screen.getByDisplayValue("Original Agent");
    expect(nameInput).toBeInTheDocument();
    fireEvent.change(nameInput, { target: { value: "Updated Agent" } });

    const saveButton = screen.getByText("Save draft");
    saveButton.click();

    await waitFor(() => {
      expect(api.agents.update).toHaveBeenCalledWith("a1", expect.anything());
    });

    const call = (api.agents.update as any).mock.calls[0];
    expect(call[0]).toBe("a1");
    const bodyStr = typeof call[1] === "string" ? call[1] : JSON.stringify(call[1]);
    expect(bodyStr).toContain("spec");
    expect(bodyStr).toContain("Updated Agent");
  });

  it("422 on save keeps edits", async () => {
    const mockAgent = {
      id: "a1",
      name: "Test Agent",
      draft_spec: {
        spec_version: 1,
        name: "Test Agent",
        instructions: "Test",
        model: { provider: "openai", name: "gpt-4" },
        tools: [],
      },
      latest_version: 1,
    };

    (api.agents.get as any).mockResolvedValueOnce(mockAgent);
    (api.versions.list as any).mockResolvedValueOnce([]);
    (api.agents.update as any).mockRejectedValueOnce(
      new ApiError(422, "Invalid input")
    );

    render(<AgentDetailPage />);

    await waitFor(() => {
      expect(api.agents.get).toHaveBeenCalledWith("a1");
    });

    const nameInput = screen.getByDisplayValue("Test Agent");
    expect(nameInput).toBeInTheDocument();
    fireEvent.change(nameInput, { target: { value: "Edited Name" } });

    const saveButton = screen.getByText("Save draft");
    saveButton.click();

    await waitFor(() => {
      expect(screen.getByText(/invalid input/i)).toBeInTheDocument();
    });

    expect(nameInput).toHaveValue("Edited Name");
  });
  const baseAgent = {
    id: "a1",
    name: "Base Agent",
    draft_spec: {
      spec_version: 1 as const,
      name: "Base Agent",
      instructions: "Help",
      model: { provider: "test", name: "m" },
      tools: [],
    },
    latest_version: 1,
  };
  const version = (n: number) => ({
    agent_id: "a1",
    version: n,
    spec: baseAgent.draft_spec,
    spec_hash: `hash${n}0000000000`,
    created_at: "2026-11-11T00:00:00Z",
    created_by: "alice",
  });

  it("Publish 201 shows version and refreshes list", async () => {
    vi.mocked(api.agents.get).mockResolvedValueOnce(baseAgent);
    vi.mocked(api.versions.list)
      .mockResolvedValueOnce([version(1)])
      .mockResolvedValueOnce([version(2), version(1)]);
    vi.mocked(api.versions.create).mockResolvedValueOnce(version(2));

    render(<AgentDetailPage />);
    const publish = await screen.findByRole("button", { name: "Publish" });
    fireEvent.click(publish);

    expect(await screen.findByText("Version 2 published")).toBeInTheDocument();
    expect(api.versions.list).toHaveBeenCalledTimes(2);
    expect(await screen.findByText(/hash2000/)).toBeInTheDocument();
  });

  it("Publish disabled while unsaved changes", async () => {
    vi.mocked(api.agents.get).mockResolvedValueOnce(baseAgent);
    vi.mocked(api.versions.list).mockResolvedValueOnce([]);
    const saved = { ...baseAgent, name: "Edited", draft_spec: { ...baseAgent.draft_spec, name: "Edited" } };
    vi.mocked(api.agents.update).mockResolvedValueOnce(saved);

    render(<AgentDetailPage />);
    const nameInput = await screen.findByDisplayValue("Base Agent");
    expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled();

    fireEvent.change(nameInput, { target: { value: "Edited" } });
    expect(await screen.findByText("Save the draft before publishing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();

    fireEvent.click(screen.getByText("Save draft"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled());
    expect(screen.queryByText("Save the draft before publishing")).not.toBeInTheDocument();
  });
});
