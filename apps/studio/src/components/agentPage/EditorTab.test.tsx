import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { setWindowWidth, TEST_WIDE_WIDTH } from "@/components/frame/frameTesting";
import { chooseVersionAction, versionActions } from "@/components/agents/versionsTesting";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { EditorTab } from "@/components/agentPage/EditorTab";

// SL.6: the editor page became the agent page's Editor tab; same body, the agent id from the route.
function AgentDetailPage() {
  return <EditorTab agentId="a1" />;
}
import { api, ApiError } from "@/lib/api";
import { useRouter, useParams } from "next/navigation";
import { compareVersions, restoreVersion } from "@/lib/agentVersions";

// RM.17: the Knowledge and Memory steps load these when shown; never reach the network.
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(() => Promise.resolve([])), listEmbeddingModels: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/agentVersions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/agentVersions")>();
  return { ...actual, compareVersions: vi.fn(), restoreVersion: vi.fn() };
});

vi.mock("next/navigation", () => ({
  // SL.11: the stepper writes `?step=` / `?compare=` with replace and reads them back.
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useParams: () => ({ id: "a1" }),
  usePathname: () => "/agents/a1/editor",
  useSearchParams: () => new URLSearchParams(),
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

vi.mock("@/components/agents/specSections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/agents/specSections")>();
  // Pending by default; the processor test resolves them with the fixtures.
  return { ...actual, fetchProcessorCatalog: vi.fn(() => new Promise(() => undefined)), fetchModels: vi.fn(() => new Promise(() => undefined)) };
});

// 412 revision_mismatch (migration 0033): the save is refused, the page offers reload or overwrite.
class FakeConflict extends Error {
  status = 412;
  key = "agent:a1";
  constructor(public currentRevision: number, public current: unknown) {
    super("changed");
  }
}

describe("AgentDetailPage concurrency", () => {
  let restoreWidth: () => void = () => undefined;
  beforeEach(() => {
    vi.clearAllMocks();
    restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  });
  afterEach(() => restoreWidth());

  const agentWith = (name: string, revision: number) => ({
    id: "a1",
    name,
    revision,
    draft_spec: { spec_version: 1, name, instructions: "Test", model: { provider: "openai", name: "gpt-4" }, tools: [] },
    latest_version: 1,
  });

  async function editAndSave() {
    (api.agents.get as any).mockResolvedValueOnce(agentWith("Mine", 1));
    (api.versions.list as any).mockResolvedValueOnce([]);
    render(<AgentDetailPage />);
    fireEvent.change(await screen.findByDisplayValue("Mine"), { target: { value: "My edit" } });
    fireEvent.click(screen.getByText("Save draft"));
    return await screen.findByTestId("conflict-notice");
  }

  it("a 412 shows the notice with 重新載入 and 覆寫, not an error", async () => {
    (api.agents.update as any).mockRejectedValueOnce(new FakeConflict(2, agentWith("Theirs", 2)));
    const notice = await editAndSave();
    expect(notice).toHaveTextContent("這份內容已被他人修改");
    expect(screen.getByTestId("conflict-reload")).toHaveTextContent("重新載入");
    expect(screen.getByTestId("conflict-overwrite")).toHaveTextContent("覆寫");
    expect(screen.queryByTestId("agent-save-error")).not.toBeInTheDocument();
  });

  it("重新載入 puts their version in the editor and discards the local edit", async () => {
    (api.agents.update as any).mockRejectedValueOnce(new FakeConflict(2, agentWith("Theirs", 2)));
    await editAndSave();
    fireEvent.click(screen.getByTestId("conflict-reload"));
    expect(await screen.findByDisplayValue("Theirs")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("My edit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("conflict-notice")).not.toBeInTheDocument();
    expect(api.agents.update).toHaveBeenCalledTimes(1);
  });

  it("覆寫 resends the same save and shows the saved result", async () => {
    (api.agents.update as any)
      .mockRejectedValueOnce(new FakeConflict(2, agentWith("Theirs", 2)))
      .mockResolvedValueOnce(agentWith("My edit", 3));
    await editAndSave();
    fireEvent.click(screen.getByTestId("conflict-overwrite"));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalledTimes(2));
    expect((api.agents.update as any).mock.calls[1][1].name).toBe("My edit");
    await waitFor(() => expect(screen.queryByTestId("conflict-notice")).not.toBeInTheDocument());
    expect(screen.getByDisplayValue("My edit")).toBeInTheDocument();
  });
});

describe("AgentDetailPage", () => {
  let restoreWidth: () => void = () => undefined;
  beforeEach(() => {
    vi.clearAllMocks();
    // SL.11: a desktop window, so the versions list is a column beside the steps (not the Sheet).
    restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  });
  afterEach(() => restoreWidth());

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

  it("Publish 422 naming a processor shows inline under that processor, not in the page alert", async () => {
    const { fetchProcessorCatalog, fetchModels } = await import("@/components/agents/specSections");
    const { processorCatalogFixture, modelsFixture } = await import("@/components/agents/specSections.fixtures");
    vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
    vi.mocked(fetchModels).mockResolvedValue(modelsFixture);
    (api.agents.get as any).mockResolvedValueOnce({
      id: "a1",
      name: "Test Agent",
      draft_spec: {
        spec_version: 1,
        name: "Test Agent",
        instructions: "Test",
        model: { provider: "test", name: "echo" },
        tools: [],
        input_processors: [
          { type: "pii_redact", config: { kinds: ["email"], action: "redact" } },
          { type: "regex_blocklist", config: { patterns: ["(a)\\1"], action: "block" } },
        ],
      },
      latest_version: 1,
    });
    (api.versions.list as any).mockResolvedValueOnce([]);
    (api.versions.create as any).mockRejectedValueOnce(
      new ApiError(422, "input_processors[1].config.patterns[0]: invalid RE2 pattern: backreferences are not supported"),
    );

    render(<AgentDetailPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Publish" }));

    const inline = await screen.findByTestId("processor-input-1-error");
    expect(inline).toHaveTextContent("invalid RE2 pattern");
    expect(screen.getByTestId("processor-input-1")).toContainElement(inline);
    expect(screen.queryByTestId("processor-input-0-error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-save-error")).not.toBeInTheDocument();
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

  it("Publish 201 with warnings lists them (AC.13)", async () => {
    vi.mocked(api.agents.get).mockResolvedValueOnce(baseAgent);
    vi.mocked(api.versions.list).mockResolvedValueOnce([version(1)]).mockResolvedValueOnce([version(2), version(1)]);
    const warning = { code: "cost_cap_unpriced_model", path: "model", message: "model: 'test:echo' has no price in the model price override file" };
    vi.mocked(api.versions.create).mockResolvedValueOnce({ ...version(2), warnings: [warning] } as ReturnType<typeof version>);

    render(<AgentDetailPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Publish" }));

    expect(await screen.findByText("Version 2 published")).toBeInTheDocument();
    expect(screen.getByTestId("publish-warnings")).toHaveTextContent(warning.message);
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

  it("a cost typed with trailing zeros (0.50) is saved in the server's form: the draft is clean and Publish is enabled (AC.13 review)", async () => {
    vi.mocked(api.agents.get).mockResolvedValueOnce(baseAgent);
    vi.mocked(api.versions.list).mockResolvedValue([]);
    // The server stores the cost normalised (eap_schemas.limits normalise_usd) and returns that.
    vi.mocked(api.agents.update).mockImplementation(async (_id, spec) => {
      const limits = (spec as unknown as { limits?: { max_cost_usd?: string } }).limits;
      const stored = limits?.max_cost_usd ? { ...spec, limits: { ...limits, max_cost_usd: String(Number(limits.max_cost_usd)) } } : spec;
      return { ...baseAgent, draft_spec: stored };
    });

    render(<AgentDetailPage />);
    await screen.findByDisplayValue("Base Agent");
    fireEvent.click(screen.getByRole("button", { name: /Limits and fallback models/ }));
    const cost = screen.getByLabelText(/^Cost \(USD/) as HTMLInputElement;
    fireEvent.change(cost, { target: { value: "0.50" } });
    // The field keeps the text as typed.
    expect(cost.value).toBe("0.50");
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();

    fireEvent.click(screen.getByText("Save draft"));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    expect((vi.mocked(api.agents.update).mock.calls[0][1] as unknown as { limits: unknown }).limits).toEqual({ max_cost_usd: "0.5" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled());
    expect(screen.queryByText("Save the draft before publishing")).not.toBeInTheDocument();
  });

  it("Compare with latest compares the saved draft against the latest version", async () => {
    vi.mocked(api.agents.get).mockResolvedValueOnce(baseAgent);
    vi.mocked(api.versions.list).mockResolvedValueOnce([version(2), version(1)]);
    vi.mocked(compareVersions).mockResolvedValueOnce({
      agent_id: "a1",
      base: { version: 2, spec: baseAgent.draft_spec, spec_hash: "h2", created_at: null, created_by: null, restored_from: null },
      target: { version: "draft", spec: baseAgent.draft_spec, spec_hash: "h2", created_at: null, created_by: null, restored_from: null },
      identical: true,
      changes: [],
      truncated: false,
      instructions_diff: [],
    });

    render(<AgentDetailPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Compare with latest (v2)" }));
    expect(await screen.findByText("v2 and Draft have the same spec.")).toBeInTheDocument();
    expect(compareVersions).toHaveBeenCalledWith("a1", 2, "draft");
  });

  it("Compare with latest is disabled while unsaved changes", async () => {
    vi.mocked(api.agents.get).mockResolvedValueOnce(baseAgent);
    vi.mocked(api.versions.list).mockResolvedValueOnce([version(1)]);
    render(<AgentDetailPage />);
    const nameInput = await screen.findByDisplayValue("Base Agent");
    fireEvent.change(nameInput, { target: { value: "Edited" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Compare with latest (v1)" })).toBeDisabled());
  });

  it("restore reloads the agent and versions and resets the editor to the restored draft", async () => {
    const restoredAgent = { ...baseAgent, latest_version: 3, draft_spec: { ...baseAgent.draft_spec, name: "Old Name" } };
    vi.mocked(api.agents.get).mockResolvedValueOnce(baseAgent).mockResolvedValueOnce(restoredAgent);
    vi.mocked(api.versions.list)
      .mockResolvedValueOnce([version(2), version(1)])
      .mockResolvedValueOnce([{ ...version(3), restored_from: 1 }, version(2), version(1)]);
    vi.mocked(restoreVersion).mockResolvedValueOnce({ ...version(3), restored_from: 1 });

    render(<AgentDetailPage />);
    await chooseVersionAction(1, "Restore this version");
    fireEvent.click(await screen.findByRole("button", { name: "Restore and publish" }));

    expect(await screen.findByText("restored from v1")).toBeInTheDocument();
    expect(await screen.findByDisplayValue("Old Name")).toBeInTheDocument();
    expect(restoreVersion).toHaveBeenCalledWith("a1", 1, { discardDraft: false });
  });
});
