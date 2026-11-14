// P0.16's Request Context page tests, migrated to the chat composer's request context popover (SL.10):
// the same schema form, JSON mode, server validation and storage behaviour, now with named presets.
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatTab } from "@/components/agentPage/ChatTab";
import { api, type Agent, type AgentVersion } from "@/lib/api";
import { listConversations } from "@/lib/conversations";
import { fetchModels } from "@/lib/metrics";
import {
  getRequestContext,
  listPresets,
  savePreset,
  validateRequestContext,
  RequestContextValidationError,
  type RequestContextInfo,
} from "@/lib/requestContext";
import { routes } from "@/lib/routes";

vi.mock("next/navigation", () => ({
  usePathname: () => null,
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { agents: { list: vi.fn(), get: vi.fn() }, versions: { list: vi.fn() } } };
});
vi.mock("@/lib/conversations", () => ({
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  listConversations: vi.fn(),
  renameConversation: vi.fn(),
}));
vi.mock("@/lib/metrics", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/metrics")>();
  return { ...actual, fetchModels: vi.fn() };
});
vi.mock("@/components/chat/turnApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/chat/turnApi")>();
  return { ...actual, startTurnWithOptions: vi.fn() };
});
vi.mock("@/lib/requestContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/requestContext")>();
  return { ...actual, getRequestContext: vi.fn(), validateRequestContext: vi.fn() };
});

const spec = {
  spec_version: 1 as const,
  name: "Support",
  instructions: "Help {{context.tier}}",
  model: { provider: "test", name: "echo" },
  tools: [],
};
const agents: Agent[] = [
  { id: "agent-1", name: "Support", draft_spec: spec, latest_version: 2 },
  { id: "agent-2", name: "Plain", draft_spec: spec, latest_version: null },
];
const versions: AgentVersion[] = [1, 2].map((version) => ({
  agent_id: "agent-1",
  version,
  spec,
  spec_hash: `h${version}`,
  created_at: "2026-11-14T00:00:00Z",
  created_by: "dev",
}));

const withFields: RequestContextInfo = {
  agent_id: "agent-1",
  version: 2,
  draft: false,
  fields: [
    { name: "tier", type: "enum", required: true, description: "Customer tier", max_length: null, values: ["gold", "silver"] },
    { name: "seats", type: "integer", required: false, description: "", max_length: null, values: [] },
  ],
  schema: {
    type: "object",
    required: ["tier"],
    properties: {
      tier: { type: "string", enum: ["gold", "silver"], description: "Customer tier" },
      seats: { type: "integer" },
    },
  },
};
const noFields: RequestContextInfo = { agent_id: "agent-2", version: null, draft: true, fields: [], schema: { type: "object", properties: {} } };

const SLOW = { timeout: 2000 };

function renderChat(agentId = "agent-1") {
  return render(<ChatTab agentId={agentId} />);
}

/** Opens the composer's request context popover once the agent and its fields are known. */
async function openContext() {
  const toggle = await screen.findByRole("button", { name: /Request context/ });
  await waitFor(() => expect(toggle).toBeEnabled());
  await waitFor(() => expect(toggle).not.toHaveAccessibleName("Request context: Context"));
  fireEvent.click(toggle);
  return screen.findByRole("dialog", { name: "Request context" });
}

async function saveAs(name: string) {
  fireEvent.click(screen.getByRole("button", { name: "Save as…" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Preset name" }), { target: { value: name } });
  fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
}

describe("Request context popover (P0.16 page tests, SL.10)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.history.replaceState(null, "", routes.agentChat("agent-1"));
    vi.mocked(api.agents.get).mockImplementation(async (id: string) => agents.find((agent) => agent.id === id)!);
    vi.mocked(api.versions.list).mockImplementation(async (id: string) => (id === "agent-1" ? versions : []));
    vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null } as never);
    vi.mocked(fetchModels).mockResolvedValue([]);
    vi.mocked(getRequestContext).mockImplementation(async (agentId) => (agentId === "agent-1" ? withFields : noFields));
    vi.mocked(validateRequestContext).mockImplementation(async (_agent, value) => {
      if (!("tier" in value)) throw new RequestContextValidationError(422, null, { tier: "is required" });
      if ("seats" in value && !Number.isInteger(value.seats)) throw new RequestContextValidationError(422, null, { seats: "must be an integer" });
      return value;
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("shows the target version's fields and schema, and the server's field errors inline", async () => {
    renderChat();
    const dialog = await openContext();
    expect(getRequestContext).toHaveBeenCalledWith("agent-1", null);
    expect(screen.getByRole("combobox", { name: "Version" })).toHaveValue("latest");
    expect(within(dialog).getByText(/checked against the latest version/)).toBeInTheDocument();
    expect(within(dialog).getByTestId("context-json-schema")).toHaveTextContent('"required"');
    expect(await screen.findByTestId("field-error-tier", {}, SLOW)).toHaveTextContent("is required");
    expect(screen.getByTestId("context-validation-status")).toHaveAttribute("data-state", "invalid");
    expect(screen.getByRole("button", { name: "Save as…" })).toBeDisabled();
    // The button reports the problem: "!" on the badge, "(problems)" in its name.
    expect(screen.getByRole("button", { name: /Request context/ })).toHaveAccessibleName(/problems/);
    expect(screen.getByTestId("composer-context-badge")).toHaveTextContent("!");
  });

  it("keeps values when switching between form and JSON, validates JSON edits, and saves a named preset", async () => {
    renderChat();
    await openContext();
    const tier = await screen.findByLabelText(/Tier/);
    fireEvent.change(tier, { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByTestId("context-validation-status")).toHaveAttribute("data-state", "valid"), SLOW);
    expect(validateRequestContext).toHaveBeenLastCalledWith("agent-1", { tier: "gold" }, null, expect.anything());

    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    const json = screen.getByLabelText("Request context JSON") as HTMLTextAreaElement;
    expect(JSON.parse(json.value)).toEqual({ tier: "gold" });

    fireEvent.change(json, { target: { value: '{"tier": "silver", "seats": "x"}' } });
    expect(await screen.findByTestId("context-json-field-errors", {}, SLOW)).toHaveTextContent("seats: must be an integer");

    fireEvent.change(json, { target: { value: "{" } });
    expect(screen.getByTestId("context-json-error")).toHaveTextContent("Enter valid JSON");
    expect(screen.getByRole("button", { name: "Form" })).toBeDisabled();

    fireEvent.change(json, { target: { value: '{"tier": "silver", "seats": 4}' } });
    fireEvent.click(screen.getByRole("button", { name: "Form" }));
    expect(screen.getByLabelText(/Tier/)).toHaveValue("silver");
    // An unbounded integer is a text field (with a numeric keyboard): its value reads as text.
    expect(screen.getByLabelText(/Seats/)).toHaveValue("4");

    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), SLOW);
    await saveAs("Silver team");
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Saved preset “Silver team”.");
    expect(listPresets("agent-1")).toEqual([expect.objectContaining({ name: "Silver team", value: { tier: "silver", seats: 4 }, version: null })]);
    expect(screen.getByRole("combobox", { name: "Preset" })).toHaveValue("Silver team");
    // The badge counts the set fields.
    expect(screen.getByTestId("composer-context-badge")).toHaveTextContent("2");
  });

  it("preselects the newest preset", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-14T09:00:00Z"));
    savePreset("agent-1", "Old", { tier: "gold" }, 1);
    vi.setSystemTime(new Date("2026-11-14T10:00:00Z"));
    savePreset("agent-1", "New", { tier: "silver" }, 1);
    vi.useRealTimers();
    renderChat();
    await openContext();
    expect(await screen.findByLabelText(/Tier/)).toHaveValue("silver");
    expect(screen.getByRole("combobox", { name: "Preset" })).toHaveValue("New");
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Using preset “New”.");
    // Picking the other one loads it.
    fireEvent.change(screen.getByRole("combobox", { name: "Preset" }), { target: { value: "Old" } });
    expect(screen.getByLabelText(/Tier/)).toHaveValue("gold");
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Loaded preset “Old”.");
  });

  it("says so when browser storage is unavailable", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("SecurityError"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("SecurityError"); });
    renderChat();
    await openContext();
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), SLOW);
    await saveAs("Gold");
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Browser storage is unavailable");
  });

  it("shows an empty state for an agent without fields and offers only the draft when unpublished", async () => {
    window.history.replaceState(null, "", routes.agentChat("agent-2"));
    renderChat("agent-2");
    const dialog = await openContext();
    expect(within(dialog).getByTestId("context-no-fields")).toHaveTextContent("The draft declares no context fields");
    expect(within(dialog).getByRole("link", { name: "Fields are defined in the editor" })).toHaveAttribute(
      "href",
      routes.agentEditor("agent-2", { step: "request-context" }),
    );
    expect(getRequestContext).toHaveBeenCalledWith("agent-2", "draft");
    const options = Array.from((screen.getByRole("combobox", { name: "Version" }) as HTMLSelectElement).options).map((option) => option.textContent);
    expect(options).toEqual(["Draft (unpublished)"]);
  });

  it("lists a preset's key the version no longer declares in form mode, with Remove", async () => {
    vi.mocked(validateRequestContext).mockImplementation(async (_agent, value) => {
      if ("region" in value) throw new RequestContextValidationError(422, null, { region: "is not a context field of this agent" });
      return value;
    });
    savePreset("agent-1", "Old", { tier: "gold", region: "eu" }, 2);
    renderChat();
    await openContext();
    const block = await screen.findByTestId("context-undeclared-keys");
    expect(block).toHaveTextContent("region");
    await waitFor(() => expect(block).toHaveTextContent("is not a context field of this agent"), SLOW);
    expect(screen.queryByTestId("field-errors-unmatched")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove region" }));
    expect(screen.queryByTestId("context-undeclared-keys")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("context-validation-status")).toHaveAttribute("data-state", "valid"), SLOW);
    expect(validateRequestContext).toHaveBeenLastCalledWith("agent-1", { tier: "gold" }, null, expect.anything());
  });

  it("shows undeclared-key errors in form mode for a version without fields", async () => {
    vi.mocked(getRequestContext).mockResolvedValue({ ...noFields, agent_id: "agent-1", version: 2, draft: false } as RequestContextInfo);
    vi.mocked(validateRequestContext).mockRejectedValue(new RequestContextValidationError(422, null, { tier: "is not a context field of this agent" }));
    savePreset("agent-1", "Old", { tier: "gold" }, 2);
    renderChat();
    await openContext();
    expect(screen.getByTestId("context-no-fields")).toHaveTextContent("declares no context fields");
    const block = await screen.findByTestId("context-undeclared-keys");
    await waitFor(() => expect(block).toHaveTextContent("tier"), SLOW);
  });

  it("sends an explicit empty string from JSON mode but not from form mode", async () => {
    renderChat();
    await openContext();
    await screen.findByLabelText(/Tier/);
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    fireEvent.change(screen.getByLabelText("Request context JSON"), { target: { value: '{"tier": "gold", "note": ""}' } });
    await waitFor(() => expect(validateRequestContext).toHaveBeenLastCalledWith("agent-1", { tier: "gold", note: "" }, null, expect.anything()), SLOW);
    fireEvent.click(screen.getByRole("button", { name: "Form" }));
    await waitFor(() => expect(validateRequestContext).toHaveBeenLastCalledWith("agent-1", { tier: "gold" }, null, expect.anything()), SLOW);
  });

  it("notes that presets are stored unencrypted", async () => {
    renderChat();
    const dialog = await openContext();
    expect(within(dialog).getByTestId("saved-context-storage-note")).toHaveTextContent("unencrypted");
  });

  it("switches to the draft from the version chip", async () => {
    renderChat();
    await openContext();
    await screen.findByLabelText(/Tier/);
    fireEvent.change(screen.getByRole("combobox", { name: "Version" }), { target: { value: "draft" } });
    await waitFor(() => expect(getRequestContext).toHaveBeenCalledWith("agent-1", "draft"));
    expect(window.location.search).toContain("version=draft");
    expect(await screen.findByText(/checked against the draft/)).toBeInTheDocument();
  });
});
