// SL.10: the chat pane (landing, docking, turn grouping) and the composer (version and model chips,
// the request context popover with presets, the output-check hint), rendered through ChatTab.
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Agent, type AgentSpec, type AgentVersion } from "@/lib/api";
import { createConversation, getConversation, listConversations } from "@/lib/conversations";
import { fetchModels } from "@/lib/metrics";
import { RequestContextValidationError, getRequestContext, listPresets, savePreset, saveContext, validateRequestContext, type RequestContextInfo } from "@/lib/requestContext";
import { listEvents, subscribeRun, type RunEvent, type RunEventData } from "@/lib/runs";
import { startTurnWithOptions } from "@/components/chat/turnApi";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { ChatTab } from "./ChatTab";
import { footerMessageIds, turnStartMessageIds } from "./ChatPane";
import { modelChipLabel } from "./ComposerModelChip";
import { versionChipLabel } from "./ComposerVersionChip";
import { CONTEXT_SHEET_QUERY, contextSummary } from "@/components/requestContext/RequestContextPopover";

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
vi.mock("@/lib/runs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/runs")>();
  return { ...actual, listEvents: vi.fn(), subscribeRun: vi.fn() };
});

const specOf = (instructions: string, extra: object = {}): AgentSpec =>
  ({ spec_version: 1, name: "Support bot", instructions, model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [], ...extra }) as AgentSpec;
const V2 = specOf("v2 text");
const AGENT: Agent = { id: "a1", name: "Support bot", draft_spec: specOf("draft text"), latest_version: 2 };
const VERSIONS: AgentVersion[] = [1, 2].map((version) => ({
  agent_id: "a1",
  version,
  spec: version === 2 ? V2 : specOf("v1 text"),
  spec_hash: `h${version}`,
  created_at: "2026-11-14T00:00:00Z",
  created_by: "dev",
}));
const TIER: RequestContextInfo = {
  agent_id: "a1",
  version: 2,
  draft: false,
  fields: [{ name: "tier", type: "enum", required: true, description: "", max_length: null, values: ["gold", "silver"] }],
  schema: { type: "object", required: ["tier"], properties: { tier: { type: "string", enum: ["gold", "silver"] } } },
};

const event = (seq: number, type: RunEvent["type"], data: object, ts = "2026-11-14T10:00:00Z"): RunEvent => ({ seq, type, data: data as RunEventData, ts });

function matchMediaFor(matching: string[]) {
  return vi.fn((query: string) => ({
    matches: matching.includes(query),
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }));
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", routes.agentChat("a1"));
  vi.mocked(api.agents.get).mockResolvedValue(AGENT);
  vi.mocked(api.versions.list).mockResolvedValue(VERSIONS);
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null } as never);
  vi.mocked(fetchModels).mockResolvedValue([
    { id: "openai:gpt-4.1-mini", provider: "openai", name: "gpt-4.1-mini", wildcard: false, source: "allowlist", description: null },
    { id: "openai:gpt-4.1", provider: "openai", name: "gpt-4.1", wildcard: false, source: "allowlist", description: null },
  ]);
  vi.mocked(getRequestContext).mockResolvedValue(TIER);
  vi.mocked(validateRequestContext).mockImplementation(async (_agent, value) => value);
  vi.mocked(subscribeRun).mockReturnValue(() => undefined);
  vi.mocked(createConversation).mockImplementation(async (agentId, title, id) => ({
    id: id ?? "c1", agent_id: agentId, title, turn_count: 0, created_at: "", updated_at: "",
  }) as never);
  vi.mocked(startTurnWithOptions).mockImplementation(async (conversationId, runId) => ({
    run_id: runId, status: "queued", display_status: "queued", agent_id: "a1", version: 2, draft: false,
    model_id: "openai:gpt-4.1-mini", model_overridden: false, conversation_id: conversationId, turn: 1,
  }) as never);
});

afterEach(() => {
  // Unmount first: a pending rail refresh must not reach a restored (unmocked) function.
  cleanup();
  vi.restoreAllMocks();
});

/** The text a chip shows (the chip also holds its hidden select's options). */
function chipLabel(chip: "version" | "model") {
  return within(screen.getByTestId(`composer-chip-${chip}`)).getByTestId("composer-chip-label");
}

async function renderChat() {
  const view = render(<ChatTab agentId="a1" />);
  const toggle = await screen.findByRole("button", { name: /Request context/ });
  await waitFor(() => expect(toggle).toHaveAccessibleName(/of 1 set/));
  return view;
}

describe("chat pane", () => {
  it("shows the landing with the composer in the middle, and docks the composer after the first send", async () => {
    await renderChat();
    const landing = screen.getByTestId("chat-landing");
    expect(within(landing).getByRole("heading", { name: "Support bot" })).toBeInTheDocument();
    expect(screen.getByTestId("chat-composer-dock")).toHaveAttribute("data-docked", "false");
    expect(screen.queryByTestId("chat-scroll-to-bottom")).toBeNull();
    const dock = screen.getByTestId("chat-composer-dock");
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(screen.getByTestId("chat-composer-dock")).toHaveAttribute("data-docked", "true"));
    // The same composer moved: it was not remounted.
    expect(screen.getByTestId("chat-composer-dock")).toBe(dock);
    expect(screen.queryByTestId("chat-landing")).toBeNull();
    expect(screen.getByTestId("chat-transcript")).toHaveTextContent("Hello");
    expect(screen.getByTestId("chat-scroll-to-bottom")).toHaveAccessibleName("Scroll to bottom");
  });

  it("groups turns: space before each later turn, the badge under each turn's last message, the user bubble on fill-selected", async () => {
    vi.mocked(getConversation).mockResolvedValue({
      id: "c1", agent_id: "a1", title: "T", turn_count: 2, created_at: "", updated_at: "",
      runs: ["r1", "r2"].map((run_id, index) => ({
        run_id, agent_id: "a1", version: 2, status: "completed", display_status: "completed", input: `q${index}`, output: null, error: null,
        heartbeat_at: null, created_at: "", updated_at: "", trace_id: null, conversation_id: "c1", turn: index + 1,
      })),
    } as never);
    vi.mocked(listEvents).mockImplementation(async (runId) => [
      event(0, "run_started", { input: `question ${runId}` }),
      event(1, "model_message", { model_request: 0, text: `answer ${runId}`, is_final: true }),
      event(2, "run_completed", {}),
    ]);
    render(<ChatTab agentId="a1" conversationId="c1" />);
    await screen.findByText("answer r2");
    expect(screen.getAllByTestId("turn-footer")).toHaveLength(2);
    const users = screen.getAllByTestId("chat-message-user");
    expect(users[0]).toHaveClass("bg-fill-selected");
    expect(users[0].closest("[data-turn-start]")).toBeNull();
    expect(users[1].closest("[data-turn-start]")).not.toBeNull();
  });

  it("keys tool-call status and duration by run: the same call id in two turns keeps its own (SL.10 review)", async () => {
    vi.mocked(getConversation).mockResolvedValue({
      id: "c1", agent_id: "a1", title: "T", turn_count: 2, created_at: "", updated_at: "",
      runs: ["r1", "r2"].map((run_id, index) => ({
        run_id, agent_id: "a1", version: 2, status: "completed", display_status: "completed", input: `q${index}`, output: null, error: null,
        heartbeat_at: null, created_at: "", updated_at: "", trace_id: null, conversation_id: "c1", turn: index + 1,
      })),
    } as never);
    // A scripted model reuses the call id per tool name: both turns call `lookup` as "call-lookup".
    vi.mocked(listEvents).mockImplementation(async (runId) => [
      event(0, "run_started", { input: `question ${runId}` }),
      event(1, "tool_call", { tool_call_id: "call-lookup", tool_name: "lookup", args: {} }, "2026-11-14T10:00:00.000Z"),
      runId === "r1"
        ? event(2, "tool_result", { tool_call_id: "call-lookup", status: "ok", content: "found" }, "2026-11-14T10:00:00.200Z")
        : event(2, "tool_result", { tool_call_id: "call-lookup", status: "error", content: "boom" }, "2026-11-14T10:00:03.000Z"),
      event(3, "model_message", { model_request: 1, text: `answer ${runId}`, is_final: true }),
      event(4, "run_completed", {}),
    ]);
    render(<ChatTab agentId="a1" conversationId="c1" />);
    await screen.findByText("answer r2");
    const cards = screen.getAllByTestId("tool-call-card");
    expect(cards).toHaveLength(2);
    expect(cards.map((card) => card.getAttribute("data-status"))).toEqual(["ok", "error"]);
    expect(within(cards[0]).getByTestId("tool-call-status")).toHaveTextContent("Done");
    expect(within(cards[0]).getByTestId("tool-call-duration")).toHaveTextContent("200 ms");
    expect(within(cards[1]).getByTestId("tool-call-status")).toHaveTextContent("Failed");
    expect(within(cards[1]).getByTestId("tool-call-duration")).toHaveTextContent("3.00 s");
  });

  it("works out turn boundaries and footers from message ids", () => {
    const messages = [{ id: "r1:user-0" }, { id: "r1:assistant-0" }, { id: "r2:user-0" }, { id: "r2:assistant-1" }, { id: "plain" }];
    expect([...footerMessageIds(messages)]).toEqual([["r1:assistant-0", "r1"], ["r2:assistant-1", "r2"]]);
    expect([...turnStartMessageIds(messages)]).toEqual(["r2:user-0", "plain"]);
  });
});

describe("composer chips", () => {
  it("labels the chips with the short choice, and lists Draft first when it holds unpublished changes", async () => {
    await renderChat();
    // Nothing pinned: the chip says it follows the latest (SL.10 review: a pinned v2 reads "v2").
    expect(chipLabel("version")).toHaveTextContent(/^Latest · v2$/);
    await screen.findByRole("option", { name: "openai:gpt-4.1" });
    expect(screen.getByTestId("composer-chip-model")).toHaveTextContent("gpt-4.1-mini");
    const options = Array.from((screen.getByRole("combobox", { name: "Version" }) as HTMLSelectElement).options).map((o) => o.textContent);
    expect(options).toEqual(["Draft (unpublished)", "Latest (v2)", "v2", "v1"]);
    fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "openai:gpt-4.1" } });
    expect(screen.getByTestId("composer-chip-model")).toHaveTextContent("gpt-4.1");
    fireEvent.change(screen.getByRole("combobox", { name: "Version" }), { target: { value: "v2" } });
    expect(chipLabel("version")).toHaveTextContent(/^v2$/);
    // The control's accessible name stays "Version"; its value tells pinned from latest apart.
    expect(screen.getByRole("combobox", { name: "Version" })).toHaveValue("v2");
    fireEvent.change(screen.getByRole("combobox", { name: "Version" }), { target: { value: "draft" } });
    expect(chipLabel("version")).toHaveTextContent(/^Draft$/);
  });

  it("lists Draft last when the draft equals the latest version", async () => {
    vi.mocked(api.agents.get).mockResolvedValue({ ...AGENT, draft_spec: V2 });
    await renderChat();
    await waitFor(() => {
      const options = Array.from((screen.getByRole("combobox", { name: "Version" }) as HTMLSelectElement).options).map((o) => o.textContent);
      expect(options).toEqual(["Latest (v2)", "v2", "v1", "Draft (unpublished)"]);
    });
  });

  it("chip and summary labels", () => {
    expect(versionChipLabel(null, 3)).toBe("Latest · v3");
    expect(versionChipLabel(3, 3)).toBe("v3");
    expect(versionChipLabel(null, null)).toBe("Latest");
    expect(versionChipLabel("draft", 3)).toBe("Draft");
    expect(versionChipLabel(2, 3)).toBe("v2");
    expect(modelChipLabel({ choice: "", customName: "" }, "openai:gpt-4.1")).toBe("gpt-4.1");
    expect(modelChipLabel({ choice: "", customName: "" }, null)).toBe("Model");
    expect(modelChipLabel({ choice: "anthropic:*", customName: " claude-x " }, null)).toBe("claude-x");
    expect(modelChipLabel({ choice: "anthropic:*", customName: "" }, null)).toBe("anthropic: …");
    expect(contextSummary(null, 0, false)).toBe("Context");
    expect(contextSummary(0, 0, false)).toBe("No context fields");
    expect(contextSummary(2, 1, true)).toBe("1 of 2 set (problems)");
  });

  it("shows the output-check hint only when the target has output processors", async () => {
    await renderChat();
    expect(screen.queryByTestId("composer-output-checked")).toBeNull();
    vi.mocked(api.versions.list).mockResolvedValue([{ ...VERSIONS[0] }, { ...VERSIONS[1], spec: specOf("v2", { output_processors: [{ type: "regex_block", patterns: ["x"] }] }) }]);
    const second = render(<ChatTab agentId="a1" />);
    await waitFor(() => expect(within(second.container).getByTestId("composer-output-checked")).toHaveTextContent("Output checked before display"));
  });
});

describe("request context popover", () => {
  it("opens from `?context=open` and drops the flag from the URL", async () => {
    window.history.replaceState(null, "", routes.agentChat("a1", { version: 1, context: "open" }));
    await renderChat();
    expect(await screen.findByRole("dialog", { name: "Request context" })).toBeInTheDocument();
    const params = new URLSearchParams(window.location.search);
    expect(params.get("context")).toBeNull();
    expect(params.get("version")).toBe("1");
  });

  it("migrates the old saved value into the preset \"Saved\" on first open, once", async () => {
    saveContext("a1", { tier: "silver" }, 2);
    await renderChat();
    expect(screen.getByRole("button", { name: /Request context/ })).toHaveAccessibleName(/1 of 1 set/);
    expect(listPresets("a1").map((preset) => preset.name)).toEqual(["Saved"]);
    fireEvent.click(screen.getByRole("button", { name: /Request context/ }));
    expect(await screen.findByRole("combobox", { name: "Preset" })).toHaveValue("Saved");
    expect(listPresets("a1")).toHaveLength(1);
  });

  it("Update overwrites the selected preset with the current value", async () => {
    savePreset("a1", "Team", { tier: "gold" }, null);
    await renderChat();
    fireEvent.click(screen.getByRole("button", { name: /Request context/ }));
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "silver" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Update" })).toBeEnabled(), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Updated preset “Team”.");
    expect(listPresets("a1")).toEqual([expect.objectContaining({ name: "Team", value: { tier: "silver" } })]);
  });

  it("saving a preset never submits the composer (the popover renders inside its form)", async () => {
    await renderChat();
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "do not send" } });
    fireEvent.click(screen.getByRole("button", { name: /Request context/ }));
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: "Save as…" }));
    const name = screen.getByRole("textbox", { name: "Preset name" });
    fireEvent.change(name, { target: { value: "Gold" } });
    fireEvent.submit(name.closest("form")!);
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Saved preset “Gold”.");
    expect(startTurnWithOptions).not.toHaveBeenCalled();
    expect(createConversation).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("do not send");
  });

  it("a name problem is shown inline and nothing is saved", async () => {
    await renderChat();
    fireEvent.click(screen.getByRole("button", { name: /Request context/ }));
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: "Save as…" }));
    fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a name");
    expect(screen.getByRole("textbox", { name: "Preset name" })).toHaveAttribute("aria-invalid", "true");
    expect(listPresets("a1")).toEqual([]);
  });

  it("a version change drops the old validation: Save as… waits for the new version's fields (SL.10 review)", async () => {
    let release: (info: RequestContextInfo) => void = () => {};
    await renderChat();
    fireEvent.click(screen.getByRole("button", { name: /Request context/ }));
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), { timeout: 2000 });
    expect(screen.getByTestId("context-validation-status")).toHaveAttribute("data-state", "valid");
    // v1's fields are still loading after the switch.
    vi.mocked(getRequestContext).mockImplementation(() => new Promise<RequestContextInfo>((resolve) => { release = resolve; }));
    fireEvent.change(screen.getByRole("combobox", { name: "Version" }), { target: { value: "v1" } });
    expect(await screen.findByText("Loading context fields…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save as…" })).toBeDisabled();
    expect(screen.queryByTestId("context-validation-status")).toBeNull();
    // Once v1's fields are known, the value is checked against them and can be saved again.
    await act(async () => release({ ...TIER, version: 1 }));
    await waitFor(() => expect(validateRequestContext).toHaveBeenLastCalledWith("a1", { tier: "gold" }, 1, expect.anything()), { timeout: 2000 });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), { timeout: 2000 });
  });

  it("an invalid result for the old version is not shown, and nothing can be saved, while the new fields fail to load (SL.10 review)", async () => {
    vi.mocked(validateRequestContext).mockImplementation(async (_agent, value) => {
      if (!("tier" in value)) throw new RequestContextValidationError(422, null, { tier: "is required" });
      return value;
    });
    await renderChat();
    fireEvent.click(screen.getByRole("button", { name: /Request context/ }));
    await screen.findByTestId("field-error-tier", {}, { timeout: 2000 });
    expect(screen.getByTestId("composer-context-badge")).toHaveTextContent("!");
    vi.mocked(getRequestContext).mockRejectedValue(new Error("down"));
    fireEvent.change(screen.getByRole("combobox", { name: "Version" }), { target: { value: "v1" } });
    expect(await screen.findByTestId("context-load-error")).toBeInTheDocument();
    // The stale "invalid" is gone (no "!" and no "problems"), and Save as… stays disabled.
    expect(screen.queryByTestId("composer-context-badge")).toBeNull();
    expect(screen.getByRole("button", { name: /Request context/ })).not.toHaveAccessibleName(/problems/);
    expect(screen.getByRole("button", { name: "Save as…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Update" })).toBeDisabled();
  });

  it("is a Sheet below 640 px", async () => {
    const original = window.matchMedia;
    // Narrow for the popover only: the panes stay wide, so ChatTab's own threads Sheet stays closed.
    window.matchMedia = matchMediaFor([CONTEXT_SHEET_QUERY, "(min-width: 1024px)"]) as unknown as typeof window.matchMedia;
    try {
      await renderChat();
      const toggle = screen.getByRole("button", { name: /Request context/ });
      fireEvent.click(toggle);
      const sheet = await screen.findByTestId("request-context-sheet");
      expect(sheet).toHaveAttribute("id", "playground-context-panel");
      expect(within(sheet).getByRole("heading", { name: "Request context" })).toBeInTheDocument();
      expect(screen.queryByTestId("request-context-popover")).toBeNull();
      fireEvent.keyDown(sheet, { key: "Escape" });
      await waitFor(() => expect(screen.queryByTestId("request-context-sheet")).toBeNull());
    } finally {
      window.matchMedia = original;
    }
  });

  it("has no axe violations on the composer and the open popover", async () => {
    const { container } = await renderChat();
    await expectNoAxeViolations(container);
    fireEvent.click(screen.getByRole("button", { name: /Request context/ }));
    await screen.findByLabelText(/Tier/);
    await act(async () => {
      await expectNoAxeViolations(document.body);
    });
  }, AXE_TIMEOUT_MS);
});
