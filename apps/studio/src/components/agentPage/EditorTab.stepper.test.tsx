import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ search: "", path: "", push: vi.fn(), replace: vi.fn(), back: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: nav.back, prefetch: vi.fn() }),
  usePathname: () => nav.path,
  useSearchParams: () => new URLSearchParams(nav.search),
  useParams: () => ({ id: "a1" }),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { get: vi.fn(), update: vi.fn(), create: vi.fn() }, versions: { list: vi.fn(), create: vi.fn() } } };
});
vi.mock("@/lib/agentVersions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/agentVersions")>();
  return { ...actual, compareVersions: vi.fn(), restoreVersion: vi.fn() };
});
vi.mock("@/lib/tools", () => ({ listTools: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(() => Promise.resolve([])), test: vi.fn() } };
});
vi.mock("@/components/agents/specSections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/agents/specSections")>();
  return { ...actual, fetchProcessorCatalog: vi.fn(), fetchModels: vi.fn() };
});

import { api, ApiError, type AgentSpec } from "@/lib/api";
import { compareVersions, restoreVersion } from "@/lib/agentVersions";
import { routes } from "@/lib/routes";
import { fetchModels, fetchProcessorCatalog } from "@/components/agents/specSections";
import { modelsFixture, processorCatalogFixture } from "@/components/agents/specSections.fixtures";
import { setWindowWidth, TEST_WIDE_WIDTH } from "@/components/frame/frameTesting";
import { chooseVersionAction, versionActions } from "@/components/agents/versionsTesting";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { EditorTab } from "./EditorTab";

const DRAFT: AgentSpec = {
  spec_version: 1,
  name: "Support bot",
  instructions: "Help people",
  model: { provider: "test", name: "echo" },
  tools: [],
  input_processors: [{ type: "max_length", config: { max_chars: 100, action: "truncate" } }],
} as AgentSpec;
const V1: AgentSpec = { ...DRAFT, instructions: "Old help" };
const version = (n: number, spec: AgentSpec) => ({ agent_id: "a1", version: n, spec, spec_hash: `hash${n}000000`, created_at: "2026-11-11T10:00:00Z", created_by: "alice" });

let restoreWidth: () => void = () => undefined;
beforeEach(() => {
  vi.clearAllMocks();
  nav.search = "";
  nav.path = routes.agentEditor("a1");
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  vi.mocked(api.agents.get).mockResolvedValue({ id: "a1", name: "Support bot", draft_spec: DRAFT, latest_version: 2 });
  // v2 equals the draft, v1 is older.
  vi.mocked(api.versions.list).mockResolvedValue([version(2, DRAFT), version(1, V1)]);
  vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
  vi.mocked(fetchModels).mockResolvedValue(modelsFixture);
});
afterEach(() => restoreWidth());

const stepNav = () => screen.getByRole("navigation", { name: "Editor steps" });
const navButton = (label: RegExp) => within(stepNav()).getByRole("button", { name: label });
const stepRegion = (label: string) => screen.getByRole("region", { name: label });

async function renderEditor() {
  const view = render(<EditorTab agentId="a1" />);
  await screen.findByTestId("agent-stepper");
  return view;
}

describe("stepped editor (SL.11)", () => {
  it("opens on ?step= and shows only that step; unknown steps open the first", async () => {
    nav.search = "step=tools";
    await renderEditor();
    expect(navButton(/^Tools/)).toHaveAttribute("aria-current", "step");
    expect(stepRegion("Tools")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Identity and model" })).toBeNull();
    expect(screen.getByLabelText("Name")).not.toBeVisible();
  });

  it("an unknown ?step= opens Identity and model", async () => {
    nav.search = "step=nope";
    await renderEditor();
    expect(navButton(/^Identity and model/)).toHaveAttribute("aria-current", "step");
  });

  it("shows status lines and checks in the nav", async () => {
    await renderEditor();
    expect(navButton(/^Identity and model/)).toHaveTextContent("test:echo");
    expect(navButton(/^Identity and model/)).toHaveAccessibleName(/complete/);
    expect(navButton(/^Tools/)).toHaveTextContent("None");
    expect(navButton(/^Processors/)).toHaveTextContent("1 processor");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "" } });
    expect(navButton(/^Identity and model/)).toHaveTextContent("Required");
    expect(navButton(/^Identity and model/)).toHaveAccessibleName(/needs attention/);
  });

  it("Previous and Next are named after the neighbouring steps and write ?step=", async () => {
    await renderEditor();
    expect(screen.queryByTestId("stepper-previous")).toBeNull();
    const next = screen.getByTestId("stepper-next");
    expect(next).toHaveAccessibleName("Next step: Instructions");
    fireEvent.click(next);
    expect(stepRegion("Instructions")).toBeVisible();
    expect(nav.replace).toHaveBeenLastCalledWith(routes.agentEditor("a1", { step: "instructions" }), { scroll: false });
    expect(screen.getByTestId("stepper-previous")).toHaveAccessibleName("Previous step: Identity and model");
    expect(screen.getByTestId("stepper-next")).toHaveAccessibleName("Next step: Tools");
    fireEvent.click(navButton(/^Processors/));
    expect(screen.queryByTestId("stepper-next")).toBeNull();
  });

  it("edits on one step survive moving to another and are saved together", async () => {
    vi.mocked(api.agents.update).mockImplementation(async (_id, spec) => ({ id: "a1", name: spec.name, draft_spec: spec, latest_version: 2 }));
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Support bot 2" } });
    fireEvent.click(navButton(/^Instructions/));
    fireEvent.change(screen.getByRole("textbox", { name: "Instructions" }), { target: { value: "Help kindly" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    expect(vi.mocked(api.agents.update).mock.calls[0][1]).toMatchObject({ name: "Support bot 2", instructions: "Help kindly" });
  });

  it("an untouched draft saves byte-identical spec JSON", async () => {
    vi.mocked(api.agents.update).mockImplementation(async (_id, spec) => ({ id: "a1", name: spec.name, draft_spec: spec, latest_version: 2 }));
    const stored = { ...DRAFT, model_settings: { temperature: 0.3, max_tokens: 200 }, description: "kept" } as AgentSpec;
    vi.mocked(api.agents.get).mockResolvedValue({ id: "a1", name: "Support bot", draft_spec: stored, latest_version: 2 });
    await renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    expect(JSON.stringify(vi.mocked(api.agents.update).mock.calls[0][1])).toBe(JSON.stringify(stored));
  });

  it("Save with a required field empty on another step opens that step instead of saving", async () => {
    await renderEditor();
    fireEvent.click(navButton(/^Instructions/));
    fireEvent.change(screen.getByRole("textbox", { name: "Instructions" }), { target: { value: "" } });
    fireEvent.click(navButton(/^Tools/));
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(stepRegion("Instructions")).toBeVisible());
    expect(api.agents.update).not.toHaveBeenCalled();
  });

  it("a publish error naming a processor opens the Processors step with the error inline", async () => {
    vi.mocked(api.versions.create).mockRejectedValueOnce(new ApiError(422, "input_processors[0].config.max_chars: must be at least 1"));
    await renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByTestId("processor-input-0-error")).toBeVisible();
    expect(stepRegion("Processors")).toBeVisible();
    expect(navButton(/^Processors/)).toHaveTextContent("Has an error");
  });

  it("Test draft opens the chat with the draft selected", async () => {
    await renderEditor();
    expect(screen.getByRole("link", { name: "Test draft" })).toHaveAttribute("href", routes.agentChat("a1", { version: "draft" }));
  });
});

describe("versions column and compare Sheet", () => {
  const compareResult = {
    agent_id: "a1",
    base: { version: 1, spec: V1, spec_hash: "h1", created_at: null, created_by: null, restored_from: null },
    target: { version: "draft", spec: DRAFT, spec_hash: "h2", created_at: null, created_by: null, restored_from: null },
    identical: false,
    changes: [{ path: "model.name", kind: "changed", before: "a", after: "b" }],
    truncated: false,
    instructions_diff: [],
  };

  it("the versions column sits beside the steps on a desktop window, with Latest and row menus", async () => {
    await renderEditor();
    const column = screen.getByRole("complementary", { name: "Versions" });
    expect(within(column).getByText("Latest")).toBeInTheDocument();
    expect(within(column).getByRole("button", { name: "Actions for version 1" })).toBeInTheDocument();
    // The draft equals v2: no Draft row.
    expect(screen.queryByTestId("version-row-draft")).toBeNull();
  });

  it("a Draft row leads the list when the draft has unpublished changes", async () => {
    vi.mocked(api.versions.list).mockResolvedValue([version(2, V1), version(1, V1)]);
    await renderEditor();
    expect(await screen.findByTestId("version-row-draft")).toHaveTextContent("Unpublished changes");
  });

  it("?compare=1..draft opens the compare Sheet with Restore v1 inside", async () => {
    vi.mocked(compareVersions).mockResolvedValue(compareResult as never);
    nav.search = "compare=1..draft";
    await renderEditor();
    const sheet = await screen.findByRole("dialog", { name: "Compare v1 with Draft" });
    expect(await within(sheet).findByText("model.name")).toBeInTheDocument();
    expect(compareVersions).toHaveBeenCalledWith("a1", 1, "draft");
    fireEvent.click(within(sheet).getByRole("button", { name: "Restore v1" }));
    expect(await screen.findByRole("dialog", { name: "Restore version 1?" })).toHaveTextContent("publishes a new version, 3");
  });

  it("a row's Compare with latest writes ?compare= and opens the Sheet", async () => {
    vi.mocked(compareVersions).mockResolvedValue({ ...compareResult, target: { ...compareResult.target, version: 2 } } as never);
    await renderEditor();
    await chooseVersionAction(1, "Compare with latest");
    // Pushed (SL.15 review M9): Back closes the compare Sheet, as it does the tool and trace Sheets.
    expect(nav.push).toHaveBeenLastCalledWith(routes.agentEditor("a1", { compare: "1..2" }), { scroll: false });
    expect(await screen.findByRole("dialog", { name: "Compare v1 with v2" })).toBeInTheDocument();
  });

  it("closing a compare Sheet it opened goes Back; one that came with the URL closes by replacing", async () => {
    vi.mocked(compareVersions).mockResolvedValue({ ...compareResult, target: { ...compareResult.target, version: 2 } } as never);
    await renderEditor();
    await chooseVersionAction(1, "Compare with latest");
    const opened = await screen.findByRole("dialog", { name: "Compare v1 with v2" });
    fireEvent.keyDown(opened, { key: "Escape" });
    await waitFor(() => expect(nav.back).toHaveBeenCalledTimes(1));
    cleanup();

    nav.back.mockClear();
    nav.replace.mockClear();
    nav.search = "compare=1..draft";
    vi.mocked(compareVersions).mockResolvedValue(compareResult as never);
    await renderEditor();
    const fromUrl = await screen.findByRole("dialog");
    fireEvent.keyDown(fromUrl, { key: "Escape" });
    await waitFor(() => expect(nav.replace).toHaveBeenLastCalledWith(routes.agentEditor("a1"), { scroll: false }));
    expect(nav.back).not.toHaveBeenCalled();
  });

  it("a malformed ?compare= opens nothing", async () => {
    nav.search = "compare=2..2";
    await renderEditor();
    await act(async () => undefined);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("phone width", () => {
  it("at 375 px the nav is a select and the versions open in a Sheet", async () => {
    restoreWidth();
    restoreWidth = setWindowWidth(375);
    await renderEditor();
    expect(screen.getByTestId("agent-stepper")).toHaveAttribute("data-layout", "compact");
    expect(screen.queryByRole("navigation", { name: "Editor steps" })).toBeNull();
    const select = screen.getByTestId("stepper-select");
    fireEvent.change(select, { target: { value: "request-context" } });
    expect(stepRegion("Request context")).toBeVisible();
    expect(screen.queryByRole("complementary", { name: "Versions" })).toBeNull();
    fireEvent.click(screen.getByTestId("versions-sheet-toggle"));
    const sheet = await screen.findByRole("dialog", { name: "Versions" });
    expect(within(sheet).getByText("Showing 2 versions")).toBeInTheDocument();
  });
});

describe("unsaved-changes guard", () => {
  it("prompts only with unsaved edits; Stay keeps the editor, Leave follows the link", async () => {
    await renderEditor();
    const link = screen.getByRole("link", { name: "Test draft" });
    // Clean: the click is not held.
    fireEvent.click(link);
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Edited" } });
    fireEvent.click(link);
    const dialog = await screen.findByRole("dialog", { name: "Leave without saving?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Stay" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(nav.push).not.toHaveBeenCalled();

    fireEvent.click(link);
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Leave without saving?" })).getByRole("button", { name: "Leave without saving" }));
    expect(nav.push).toHaveBeenCalledWith(routes.agentChat("a1", { version: "draft" }));
  });

  it("asks the browser before unloading only while dirty", async () => {
    await renderEditor();
    const clean = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(clean);
    expect(clean.defaultPrevented).toBe(false);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Edited" } });
    const dirty = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(dirty);
    expect(dirty.defaultPrevented).toBe(true);
  });

  it("step links within the editor are not guarded", async () => {
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Edited" } });
    const a = document.createElement("a");
    a.href = routes.agentEditor("a1", { step: "tools" });
    document.body.appendChild(a);
    fireEvent.click(a);
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    a.remove();
  });
});

describe("create mode", () => {
  it("/agents/new uses the stepper with Create agent and goes to the new agent's editor", async () => {
    nav.path = routes.agentNew();
    const { default: NewAgentPage } = await import("@/app/agents/new/page");
    vi.mocked(api.agents.create).mockResolvedValue({ id: "new1", name: "N", draft_spec: DRAFT, latest_version: null });
    render(<NewAgentPage />);
    await screen.findByTestId("agent-stepper");
    expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    // Name is empty: nothing is sent.
    expect(api.agents.create).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "N" } });
    fireEvent.change(screen.getByLabelText("Model provider"), { target: { value: "test" } });
    fireEvent.change(screen.getByLabelText("Model name"), { target: { value: "echo" } });
    fireEvent.click(navButton(/^Instructions/));
    fireEvent.change(screen.getByRole("textbox", { name: "Instructions" }), { target: { value: "Hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() => expect(api.agents.create).toHaveBeenCalled());
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(routes.agentEditor("new1")));
    nav.path = routes.agentEditor("a1");
  });
});

describe("accessibility", () => {
  it("the stepped editor has no axe violations", async () => {
    await renderEditor();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("SL.11 review fixes", () => {
  it("I1: typing while a save is pending keeps the editor dirty afterwards", async () => {
    let finish: (value: unknown) => void = () => undefined;
    vi.mocked(api.agents.update).mockImplementation(
      (_id, spec) => new Promise((resolve) => (finish = () => resolve({ id: "a1", name: spec.name, draft_spec: spec, latest_version: 2 }))) as never,
    );
    await renderEditor();
    const name = screen.getByLabelText("Name");
    fireEvent.change(name, { target: { value: "Saved name" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    // Typed while the save is in flight.
    fireEvent.change(name, { target: { value: "Saved name, then more" } });
    await act(async () => finish(undefined));
    expect(await screen.findByText("Save the draft before publishing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    fireEvent.click(screen.getByRole("link", { name: "Test draft" }));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
  });

  it("I1: a save with no edits during it leaves the editor clean", async () => {
    vi.mocked(api.agents.update).mockImplementation(async (_id, spec) => ({ id: "a1", name: spec.name, draft_spec: spec, latest_version: 2 }));
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Saved name" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(screen.queryByText("Save the draft before publishing")).toBeNull());
    expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled();
  });

  it("M3: an MCP server error shows once, in the Tools step, not at the top", async () => {
    vi.mocked(api.versions.create).mockRejectedValueOnce(new ApiError(422, "MCP server 'x' is disabled"));
    await renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    // The error opens the Tools step (after the alert first renders).
    await waitFor(() => expect(stepRegion("Tools")).toBeVisible());
    const alerts = screen.getAllByTestId("agent-save-error");
    expect(alerts).toHaveLength(1);
    expect(stepRegion("Tools")).toContainElement(alerts[0]);
    expect(alerts[0]).toHaveTextContent("MCP server 'x' is disabled");
    expect(navButton(/^Tools/)).toHaveTextContent("Has an error");
  });

  it("M3: an error no step owns stays at the top", async () => {
    vi.mocked(api.versions.create).mockRejectedValueOnce(new ApiError(500, "Internal error"));
    await renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    const alert = await screen.findByTestId("agent-save-error");
    expect(alert.closest("section[data-step]")).toBeNull();
    expect(alert).toHaveTextContent("Internal error");
  });

  it("M4: number fields are checked inline and block Save at the field", async () => {
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Max tokens (optional)"), { target: { value: "1.5" } });
    expect(screen.getByTestId("maxTokens-problem")).toHaveTextContent("whole number");
    fireEvent.change(screen.getByLabelText("Temperature (0–2)"), { target: { value: "3" } });
    expect(screen.getByTestId("temperature-problem")).toHaveTextContent("from 0 to 2");
    fireEvent.click(navButton(/^Tools/));
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(screen.getByLabelText("Temperature (0–2)")).toHaveFocus());
    expect(stepRegion("Identity and model")).toBeVisible();
    expect(api.agents.update).not.toHaveBeenCalled();
  });

  it("M5: with unsaved edits, compares against the draft wait for a save", async () => {
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Edited" } });
    expect(screen.getByRole("button", { name: "Compare draft with latest" })).toBeDisabled();
    expect(screen.getByText("Save the draft to compare it.", { selector: "#compare-draft-hint" })).toBeInTheDocument();
    fireEvent.keyDown(await screen.findByRole("button", { name: "Actions for version 1" }), { key: "Enter" });
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "Compare with draft" })).toHaveAttribute("data-disabled");
    expect(within(menu).getByRole("menuitem", { name: "Compare with latest" })).not.toHaveAttribute("data-disabled");
  });

  it("M6: Test in chat from a row menu with unsaved edits closes the menu, then asks", async () => {
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Edited" } });
    expect(await versionActions(1)).toContain("Test in chat");
    await chooseVersionAction(1, "Test in chat");
    // A DOM query: the leave dialog aria-hides the rest of the page, so a role query would miss an open menu.
    const menuInDom = () => document.querySelector('[role="menu"]');
    expect(menuInDom()).toBeNull();
    const stay = await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(menuInDom()).toBeNull();
    fireEvent.click(within(stay).getByRole("button", { name: "Stay" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull());
    expect(menuInDom()).toBeNull();
    expect(screen.queryByRole("menu", { hidden: true })).toBeNull();
    expect(nav.push).not.toHaveBeenCalled();

    await chooseVersionAction(1, "Test in chat");
    const dialog = await screen.findByRole("dialog", { name: "Leave without saving?" });
    expect(menuInDom()).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(routes.agentChat("a1", { version: 1 })));
  });

  it("M6: Test in chat stays a real link (new tab with a modified click, no prompt)", async () => {
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Edited" } });
    const trigger = await screen.findByRole("button", { name: "Actions for version 1" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    const item = within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Test in chat" });
    expect(item).toHaveAttribute("href", routes.agentChat("a1", { version: 1 }));
    fireEvent.click(item, { metaKey: true });
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("M7: the dirty-draft 409 offers Discard draft and restore from the compare Sheet", async () => {
    vi.mocked(compareVersions).mockResolvedValue({
      agent_id: "a1",
      base: { version: 1, spec: V1, spec_hash: "h1", created_at: null, created_by: null, restored_from: null },
      target: { version: "draft", spec: DRAFT, spec_hash: "h2", created_at: null, created_by: null, restored_from: null },
      identical: false, changes: [], truncated: false, instructions_diff: [],
    } as never);
    vi.mocked(restoreVersion)
      .mockRejectedValueOnce(new ApiError(409, "the draft has unpublished changes since version 2; publish them, or restore with discard_draft: true"))
      .mockResolvedValueOnce({ ...version(3, V1), restored_from: 1 } as never);
    nav.search = "compare=1..draft";
    await renderEditor();
    const sheet = await screen.findByRole("dialog", { name: "Compare v1 with Draft" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Restore v1" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore and publish" }));
    const discard = await screen.findByRole("dialog", { name: "Discard draft and restore version 1?" });
    fireEvent.click(within(discard).getByRole("button", { name: "Discard draft and restore" }));
    await waitFor(() => expect(restoreVersion).toHaveBeenLastCalledWith("a1", 1, { discardDraft: true }));
    expect(await screen.findByText("Version 3 published, restored from v1")).toBeInTheDocument();
    expect(nav.replace).toHaveBeenLastCalledWith(routes.agentEditor("a1"), { scroll: false });
  });

  it("M7: the dirty-draft 409 offers Discard draft and restore from a row in the stepper", async () => {
    vi.mocked(restoreVersion)
      .mockRejectedValueOnce(new ApiError(409, "the draft has unpublished changes since version 2; publish them"))
      .mockResolvedValueOnce({ ...version(3, V1), restored_from: 1 } as never);
    await renderEditor();
    await chooseVersionAction(1, "Restore this version");
    fireEvent.click(await screen.findByRole("button", { name: "Restore and publish" }));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Discard draft and restore version 1?" })).getByRole("button", { name: "Discard draft and restore" }));
    await waitFor(() => expect(restoreVersion).toHaveBeenLastCalledWith("a1", 1, { discardDraft: true }));
    await waitFor(() => expect(api.agents.get).toHaveBeenCalledTimes(2));
  });

  it("M8: a step change the user starts focuses the new step's heading; an error-opened step does not", async () => {
    vi.mocked(api.versions.create).mockRejectedValueOnce(new ApiError(422, "input_processors[0].config.max_chars: must be at least 1"));
    await renderEditor();
    fireEvent.click(navButton(/^Tools/));
    expect(screen.getByRole("heading", { level: 2, name: "Tools" })).toHaveFocus();
    fireEvent.click(screen.getByTestId("stepper-next"));
    expect(screen.getByRole("heading", { level: 2, name: "Request context" })).toHaveFocus();
    const publish = screen.getByRole("button", { name: "Publish" });
    publish.focus();
    fireEvent.click(publish);
    await screen.findByTestId("processor-input-0-error");
    expect(screen.getByRole("heading", { level: 2, name: "Processors" })).not.toHaveFocus();
  });
});
