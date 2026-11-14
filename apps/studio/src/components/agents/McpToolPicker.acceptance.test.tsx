import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EditorTab } from "@/components/agentPage/EditorTab";

// SL.6: the editor page became the agent page's Editor tab; same body, the agent id from the route.
function AgentDetailPage() {
  return <EditorTab agentId="a1" />;
}
import { api, ApiError, type AgentSpec } from "@/lib/api";
import { listTools } from "@/lib/tools";
import { mcpServers, type McpServer } from "@/lib/mcpServers";
import { toolRunLedger } from "@/lib/toolRunLedger";

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

vi.mock("@/lib/tools", () => ({ listTools: vi.fn() }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(), test: vi.fn() } };
});

const docs: McpServer = {
  id: "srv-1",
  name: "docs",
  url: "https://mcp.example.com/mcp",
  transport: "streamable_http",
  enabled: true,
  header_names: [],
  created_at: null,
  updated_at: null,
  last_checked_at: null,
  last_status: null,
  last_error_code: null,
};

function spec(mcpTools?: AgentSpec["mcp_tools"]): AgentSpec {
  return {
    spec_version: 1,
    name: "Test Agent",
    instructions: "Help the operator",
    model: { provider: "openai", name: "gpt-4" },
    tools: [],
    ...(mcpTools ? { mcp_tools: mcpTools } : {}),
  };
}

function renderAgent(draft: AgentSpec) {
  vi.mocked(api.agents.get).mockResolvedValue({
    id: "a1",
    name: draft.name,
    draft_spec: draft,
    latest_version: 1,
  });
  vi.mocked(api.versions.list).mockResolvedValue([]);
  render(<AgentDetailPage />);
}

// SL.11: the MCP picker is on the stepped editor's Tools step.
async function openToolsStep() {
  const nav = await screen.findByRole("navigation", { name: "Editor steps" });
  fireEvent.click(within(nav).getByRole("button", { name: /^Tools/ }));
}

describe("Agent MCP tool picker acceptance", () => {
  beforeEach(() => {
    toolRunLedger.reset();
    vi.mocked(api.agents.get).mockReset();
    vi.mocked(api.agents.update).mockReset();
    vi.mocked(api.versions.list).mockReset();
    vi.mocked(api.versions.create).mockReset();
    vi.mocked(listTools).mockReset();
    vi.mocked(mcpServers.list).mockReset();
    vi.mocked(mcpServers.test).mockReset();
    vi.mocked(listTools).mockResolvedValue([]);
    vi.mocked(mcpServers.list).mockResolvedValue([docs]);
    vi.mocked(api.agents.update).mockImplementation(async (_id, next) => ({
      id: "a1",
      name: next.name,
      draft_spec: next,
      latest_version: 1,
    }));
  });

  it("loads tools into checkboxes, adds a chip, and adds a typed name", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({
      status: "ok",
      tools: [{ name: "search", description: "Search the docs", input_schema: {} }],
    });
    renderAgent(spec());
    await screen.findByDisplayValue("Test Agent");
    await openToolsStep();
    const picker = await screen.findByTestId("mcp-picker-server");
    fireEvent.click(within(picker).getByRole("button", { name: "Load tools" }));
    fireEvent.click(await screen.findByLabelText("docs/search"));

    expect(screen.getByTestId("mcp-selected-tool")).toHaveTextContent("docs/search");
    fireEvent.change(screen.getByLabelText("Tool name on docs"), { target: { value: "fetch_page" } });
    fireEvent.click(within(picker).getByRole("button", { name: "Add" }));
    expect(screen.getAllByTestId("mcp-selected-tool").map((chip) => chip.textContent)).toEqual([
      expect.stringContaining("docs/search"),
      expect.stringContaining("docs/fetch_page"),
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    expect(vi.mocked(api.agents.update).mock.calls[0][1].mcp_tools).toEqual([
      { server: "docs", tool: "search" },
      { server: "docs", tool: "fetch_page" },
    ]);
  });

  it("drops mcp_tools from the saved draft when every tool is unpicked", async () => {
    renderAgent(spec([{ server: "docs", tool: "search" }, { server: "docs", tool: "fetch_page" }]));
    await screen.findByDisplayValue("Test Agent");
    await openToolsStep();
    fireEvent.click(await screen.findByRole("button", { name: "Remove docs/search" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove docs/fetch_page" }));
    expect(screen.queryByTestId("mcp-selected-tool")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    const saved = vi.mocked(api.agents.update).mock.calls[0][1];
    expect(saved).not.toHaveProperty("mcp_tools");
    expect(JSON.stringify(saved)).not.toContain("mcp_tools");
  });

  it("keeps existing mcp_tools when the draft is saved untouched", async () => {
    const tools = [{ server: "docs", tool: "search" }];
    renderAgent(spec(tools));
    await screen.findByDisplayValue("Test Agent");
    expect(await screen.findByTestId("mcp-selected-tool")).toHaveTextContent("docs/search");
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    expect(vi.mocked(api.agents.update).mock.calls[0][1].mcp_tools).toEqual(tools);
  });

  it("warns when a selected server is disabled or not registered", async () => {
    vi.mocked(mcpServers.list).mockResolvedValue([{ ...docs, enabled: false }]);
    renderAgent(spec([
      { server: "docs", tool: "search" },
      { server: "ghost", tool: "lookup" },
    ]));
    await screen.findByDisplayValue("Test Agent");
    // The servers list loads separately from the agent; the warnings appear once it has settled.
    expect(await screen.findByText(/No enabled MCP servers/)).toBeInTheDocument();
    await waitFor(() => {
      const chips = screen.getAllByTestId("mcp-selected-tool");
      expect(chips.find((chip) => chip.textContent?.includes("docs/search"))).toHaveTextContent("server disabled");
      expect(chips.find((chip) => chip.textContent?.includes("ghost/lookup"))).toHaveTextContent("server not registered");
    });
    expect(screen.queryByLabelText("Tool name on docs")).not.toBeInTheDocument();
  });

  it("shows a publish 422 when an MCP server is disabled", async () => {
    renderAgent(spec([{ server: "x", tool: "search" }]));
    vi.mocked(api.versions.create).mockRejectedValue(new ApiError(422, "MCP server 'x' is disabled"));
    const publish = await screen.findByRole("button", { name: "Publish" });
    await waitFor(() => expect(publish).toBeEnabled());
    fireEvent.click(publish);

    const alert = await screen.findByTestId("agent-save-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("MCP server 'x' is disabled");
  });
});
