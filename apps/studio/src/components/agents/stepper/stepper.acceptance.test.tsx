// SL.11 acceptance: deep links, navigation, saving, the unsaved-changes guard, versions, layouts, the
// step registry contract and axe. The implementer's EditorTab.stepper.test.tsx and
// stepper.model.test.ts cover the basics; this file adds what they leave out.
import React from "react";
import "@testing-library/jest-dom";
import { createHash } from "node:crypto";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ search: "", path: "", push: vi.fn(), replace: vi.fn() }));
// RM.17: the Knowledge and Memory steps load these when shown; never reach the network.
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(() => Promise.resolve([])), listEmbeddingModels: vi.fn(() => Promise.resolve([])) }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
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
import { CORE_STEP_IDS, fetchModels, fetchProcessorCatalog } from "@/components/agents/specSections";
import { modelsFixture, processorCatalogFixture } from "@/components/agents/specSections.fixtures";
import { mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH } from "@/components/frame/frameTesting";
import { SidePanel, SidePanelProvider } from "@/components/frame/SidePanel";
import { chooseVersionAction, versionActions } from "@/components/agents/versionsTesting";
import { ConfigPanel } from "@/components/agentPage/ConfigPanel";
import { EditorTab } from "@/components/agentPage/EditorTab";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { AgentEditor } from "../AgentEditor";
import { EDITOR_STEPS } from "./registry";
import type { EditorStepDefinition, StepProps } from "./types";

const DRAFT = {
  spec_version: 1,
  name: "Support bot",
  instructions: "Help people",
  model: { provider: "test", name: "echo" },
  tools: [],
  input_processors: [{ type: "max_length", config: { max_chars: 100, action: "truncate" } }],
} as AgentSpec;
const V1: AgentSpec = { ...DRAFT, instructions: "Old help" };
const version = (n: number, spec: AgentSpec) => ({ agent_id: "a1", version: n, spec, spec_hash: `hash${n}000000`, created_at: "2026-11-11T10:00:00Z", created_by: "alice" });
const agentWith = (spec: AgentSpec) => ({ id: "a1", name: spec.name, draft_spec: spec, latest_version: 2 });
const compareResult = (base: number | "draft", target: number | "draft") => ({
  agent_id: "a1",
  base: { version: base, spec: V1, spec_hash: "h1", created_at: null, created_by: null, restored_from: null },
  target: { version: target, spec: DRAFT, spec_hash: "h2", created_at: null, created_by: null, restored_from: null },
  identical: false,
  changes: [{ path: "model.name", kind: "changed", before: "old", after: "echo" }],
  truncated: false,
  instructions_diff: [],
});

let restoreWidth: () => void = () => undefined;
beforeEach(() => {
  vi.clearAllMocks();
  nav.search = "";
  nav.path = routes.agentEditor("a1");
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  vi.mocked(api.agents.get).mockResolvedValue(agentWith(DRAFT));
  vi.mocked(api.agents.update).mockImplementation(async (_id, spec) => agentWith(spec));
  vi.mocked(api.versions.list).mockResolvedValue([version(2, DRAFT), version(1, V1)]);
  vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
  vi.mocked(fetchModels).mockResolvedValue(modelsFixture);
  vi.mocked(compareVersions).mockImplementation(async (_id, a, b) => compareResult(a as number, b as number | "draft") as never);
});
afterEach(() => restoreWidth());

const stepNav = () => screen.getByRole("navigation", { name: "Editor steps" });
const navButton = (label: RegExp) => within(stepNav()).getByRole("button", { name: label });
// With a Sheet open Radix hides the page behind it from the accessibility tree.
const navButtonBehind = (label: RegExp) => within(screen.getByTestId("stepper-nav")).getByRole("button", { name: label, hidden: true });
const region = (label: string) => screen.getByRole("region", { name: label });
const stepper = () => screen.getByTestId("agent-stepper");

async function renderEditor(ui: React.ReactElement = <EditorTab agentId="a1" />) {
  const view = render(ui);
  await screen.findByTestId("agent-stepper");
  return view;
}
const lastReplace = () => {
  const url = vi.mocked(nav.replace).mock.calls.at(-1)?.[0] as string;
  const [path, query = ""] = url.split("?");
  return { path, params: new URLSearchParams(query) };
};
/** The latest URL written by push or replace (the compare Sheet opens with a push since SL.15 M9). */
const lastWrite = () => {
  const calls = [vi.mocked(nav.push), vi.mocked(nav.replace)].flatMap((fn) => fn.mock.calls.map((call, i) => ({ url: call[0] as string, order: fn.mock.invocationCallOrder[i] })));
  const url = calls.sort((a, b) => a.order - b.order).at(-1)?.url ?? "";
  const [path, query = ""] = url.split("?");
  return { path, params: new URLSearchParams(query) };
};
const sha = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex");

describe("deep links and reload", () => {
  it("?step= and ?compare= together open the step and the Sheet, and survive a reload", async () => {
    nav.search = "step=request-context&compare=1..draft";
    const first = await renderEditor();
    expect(navButtonBehind(/^Request context/)).toHaveAttribute("aria-current", "step");
    expect(await screen.findByRole("dialog", { name: "Compare v1 with Draft" })).toBeInTheDocument();
    first.unmount();
    await renderEditor();
    expect(navButtonBehind(/^Request context/)).toHaveAttribute("aria-current", "step");
    expect(await screen.findByRole("dialog", { name: "Compare v1 with Draft" })).toBeInTheDocument();
    expect(compareVersions).toHaveBeenLastCalledWith("a1", 1, "draft");
  });

  it.each(["", "TOOLS", "tools ", "__proto__", "constructor", "6", "request_context"])("a malformed ?step=%j opens Identity and model", async (value) => {
    nav.search = `step=${encodeURIComponent(value)}`;
    await renderEditor();
    expect(navButton(/^Identity and model/)).toHaveAttribute("aria-current", "step");
    expect(region("Identity and model")).toBeVisible();
  });

  it.each(["1..", "..2", "a..b", "0..2", "1..1", "1...2", "1..2..3", "draft..draft", "-1..2", "1..2x", "1,2", "%20..2"])("a malformed ?compare=%j opens nothing", async (value) => {
    nav.search = `compare=${encodeURIComponent(value)}`;
    await renderEditor();
    await act(async () => undefined);
    expect(screen.queryByTestId("compare-sheet")).toBeNull();
    expect(compareVersions).not.toHaveBeenCalled();
  });

  it("a valid compare beside a malformed step still opens the Sheet on the first step", async () => {
    nav.search = "step=nope&compare=1..2";
    await renderEditor();
    expect(await screen.findByRole("dialog", { name: "Compare v1 with v2" })).toBeInTheDocument();
    expect(navButtonBehind(/^Identity and model/)).toHaveAttribute("aria-current", "step");
  });

  it("changing a step keeps the other query parameters and writes with replace", async () => {
    nav.search = "foo=1&step=tools";
    window.history.replaceState(null, "", `${nav.path}?${nav.search}`);
    await renderEditor();
    fireEvent.click(screen.getByTestId("stepper-next"));
    const { path, params } = lastReplace();
    expect(path).toBe(routes.agentEditor("a1"));
    expect(params.get("step")).toBe("request-context");
    expect(params.get("foo")).toBe("1");
    expect(nav.push).not.toHaveBeenCalled();
    window.history.replaceState(null, "", "/");
  });

  it("closing the Sheet removes ?compare= and keeps ?step=", async () => {
    nav.search = "step=tools&compare=1..2";
    window.history.replaceState(null, "", `${nav.path}?${nav.search}`);
    await renderEditor();
    const sheet = await screen.findByRole("dialog", { name: "Compare v1 with v2" });
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(nav.replace).toHaveBeenCalled());
    const { params } = lastReplace();
    expect(params.get("compare")).toBeNull();
    expect(params.get("step")).toBe("tools");
    window.history.replaceState(null, "", "/");
  });
});

describe("step navigation", () => {
  it("Previous and Next walk all seven steps in order with matching names and headings", async () => {
    await renderEditor();
    const labels = ["Identity and model", "Instructions", "Tools", "Request context", "Processors", "Knowledge", "Memory"];
    for (let index = 0; index < labels.length; index++) {
      expect(region(labels[index])).toBeVisible();
      expect(navButton(new RegExp(`^${labels[index]}`))).toHaveAttribute("aria-current", "step");
      expect(screen.queryByTestId("stepper-previous") === null).toBe(index === 0);
      expect(screen.queryByTestId("stepper-next") === null).toBe(index === labels.length - 1);
      if (index > 0) expect(screen.getByTestId("stepper-previous")).toHaveAccessibleName(`Previous step: ${labels[index - 1]}`);
      if (index < labels.length - 1) {
        expect(screen.getByTestId("stepper-next")).toHaveAccessibleName(`Next step: ${labels[index + 1]}`);
        fireEvent.click(screen.getByTestId("stepper-next"));
      }
    }
    fireEvent.click(screen.getByTestId("stepper-previous"));
    expect(region("Knowledge")).toBeVisible();
  });

  it("only the active step's body is visible; the others stay mounted", async () => {
    await renderEditor();
    fireEvent.click(navButton(/^Tools/));
    const bodies = Array.from(stepper().querySelectorAll("section[data-step]"));
    expect(bodies.map((body) => body.getAttribute("data-step"))).toEqual(EDITOR_STEPS.map((step) => step.id));
    expect(bodies.filter((body) => !(body as HTMLElement).hidden).map((body) => body.getAttribute("data-step"))).toEqual(["tools"]);
  });

  it("the nav's status lines and checks follow the fields as they change", async () => {
    await renderEditor();
    const identity = () => navButton(/^Identity and model/);
    const instructions = () => navButton(/^Instructions/);
    expect(identity()).toHaveTextContent("test:echo");
    expect(instructions()).toHaveTextContent("2 words");
    fireEvent.change(screen.getByLabelText("Model provider"), { target: { value: "" } });
    expect(identity()).toHaveTextContent("Required");
    expect(identity()).toHaveAccessibleName(/needs attention/);
    fireEvent.change(screen.getByLabelText("Model provider"), { target: { value: "openai" } });
    expect(identity()).toHaveTextContent("openai:echo");
    expect(identity()).toHaveAccessibleName(/complete/);
    fireEvent.click(instructions());
    const box = screen.getByRole("textbox", { name: "Instructions" });
    fireEvent.change(box, { target: { value: "Help people kindly today" } });
    expect(instructions()).toHaveTextContent("4 words");
    fireEvent.change(box, { target: { value: "Help {{context.plan}}" } });
    expect(instructions()).toHaveTextContent("1 problem");
    expect(instructions()).toHaveAccessibleName(/needs attention/);
    fireEvent.change(box, { target: { value: "" } });
    expect(instructions()).toHaveTextContent("Required");
  });

  it("at 375 px the step select lists every step with its status and switches steps", async () => {
    restoreWidth();
    restoreWidth = setWindowWidth(375);
    nav.search = "step=tools";
    await renderEditor();
    const select = screen.getByTestId("stepper-select") as HTMLSelectElement;
    expect(select.value).toBe("tools");
    expect(screen.getByText("Step 3 of 7")).toBeInTheDocument();
    expect(Array.from(select.options).map((option) => option.value)).toEqual(EDITOR_STEPS.map((step) => step.id));
    expect(select.options[0].textContent).toContain("test:echo");
    fireEvent.change(screen.getByRole("textbox", { name: "Instructions", hidden: true }), { target: { value: "" } });
    expect(select.options[1].textContent).toContain("Required");
    fireEvent.change(select, { target: { value: "processors" } });
    expect(region("Processors")).toBeVisible();
    expect(lastReplace().params.get("step")).toBe("processors");
    expect(screen.getByText("Step 5 of 7")).toBeInTheDocument();
  });
});

describe("saving", () => {
  const save = () => fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  const edit = () => fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Support bot 2" } });

  it.each([
    ["input_processors[0].config.max_chars: must be at least 1", "Processors"],
    ["instructions: placeholder {{context.x}} names no context field", "Instructions"],
    ["name: must not be empty", "Identity and model"],
    ["Model test:gone is not in the allowlist", "Identity and model"],
    ["mcp_tools[0]: unknown MCP server 'docs'", "Tools"],
  ])("a save error %j opens the %s step", async (message, label) => {
    vi.mocked(api.agents.update).mockRejectedValueOnce(new ApiError(422, message));
    nav.search = label === "Identity and model" ? "step=request-context" : "";
    await renderEditor();
    edit();
    save();
    await waitFor(() => expect(region(label)).toBeVisible());
    expect(navButton(new RegExp(`^${label}`))).toHaveTextContent("Has an error");
    expect(navButton(new RegExp(`^${label}`))).toHaveAccessibleName(/has an error/);
  });

  it("a save error no step owns stays on the step and shows the alert", async () => {
    vi.mocked(api.agents.update).mockRejectedValueOnce(new ApiError(500, "Internal server error"));
    nav.search = "step=tools";
    await renderEditor();
    edit();
    save();
    expect(await screen.findByTestId("agent-save-error")).toHaveTextContent("Internal server error");
    expect(region("Tools")).toBeVisible();
    expect(within(stepNav()).queryByText("Has an error")).toBeNull();
  });

  it("a later successful save clears the error and the draft is clean again", async () => {
    vi.mocked(api.agents.update).mockRejectedValueOnce(new ApiError(422, "name: must not be empty"));
    await renderEditor();
    edit();
    save();
    await waitFor(() => expect(navButton(/^Identity and model/)).toHaveTextContent("Has an error"));
    save();
    await waitFor(() => expect(screen.queryByText("Save the draft before publishing")).toBeNull());
    expect(navButton(/^Identity and model/)).not.toHaveTextContent("Has an error");
    expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled();
  });

  it.each([
    ["name", "Name", "Identity and model"],
    ["provider", "Model provider", "Identity and model"],
    ["modelName", "Model name", "Identity and model"],
    ["instructions", "Instructions", "Instructions"],
  ])("Save with %s empty opens its step, focuses the field and sends nothing", async (_id, fieldLabel, stepLabel) => {
    nav.search = "step=processors";
    await renderEditor();
    fireEvent.click(navButton(/^Identity and model/));
    if (fieldLabel === "Instructions") {
      fireEvent.click(navButton(/^Instructions/));
      fireEvent.change(screen.getByRole("textbox", { name: "Instructions" }), { target: { value: " " } });
    } else {
      fireEvent.change(screen.getByLabelText(fieldLabel), { target: { value: " " } });
    }
    fireEvent.click(navButton(/^Processors/));
    save();
    await waitFor(() => expect(region(stepLabel)).toBeVisible());
    await waitFor(() => expect(document.activeElement).toBe(within(region(stepLabel)).getByLabelText(fieldLabel === "Instructions" ? /Instructions/ : fieldLabel)));
    expect(api.agents.update).not.toHaveBeenCalled();
  });
});

describe("byte-identical saves and unknown keys", () => {
  // Key order is deliberately not the editor's own: instructions first, unknown keys in between.
  const STORED = {
    instructions: "Help people",
    spec_version: 1,
    description: "Answers support questions",
    name: "Support bot",
    // A valid memory config (RM.17 validates it), with a non-default observer window.
    memory: { observational: { model: { provider: "test", name: "echo" }, observe_after_tokens: 20000, keep_recent_turns: 4, reflect_after_tokens: 8000 } },
    model: { name: "echo", provider: "test", region: "eu" },
    tools: ["clock"],
    model_settings: { max_tokens: 200, temperature: 0.3, top_p: 0.9 },
    input_processors: [{ type: "max_length", config: { max_chars: 100, action: "truncate" } }],
  } as unknown as AgentSpec;
  const saved = () => vi.mocked(api.agents.update).mock.calls[0][1];
  const save = () => fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

  beforeEach(() => vi.mocked(api.agents.get).mockResolvedValue(agentWith(STORED)));

  it("an untouched draft saves the same JSON and the same hash", async () => {
    await renderEditor();
    save();
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    expect(JSON.stringify(saved())).toBe(JSON.stringify(STORED));
    expect(sha(saved())).toBe(sha(STORED));
  });

  it("visiting every step without editing still saves the same hash", async () => {
    await renderEditor();
    for (const step of EDITOR_STEPS) fireEvent.click(navButton(new RegExp(`^${step.label}`)));
    save();
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    expect(sha(saved())).toBe(sha(STORED));
  });

  it("unknown keys survive an edit: only the edited key changes, order kept", async () => {
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Support bot 2" } });
    save();
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    const spec = saved() as unknown as Record<string, unknown>;
    expect(Object.keys(spec)).toEqual(Object.keys(STORED));
    expect(spec.description).toBe("Answers support questions");
    expect(spec.memory).toEqual((STORED as unknown as Record<string, unknown>).memory);
    expect(spec.model).toEqual({ name: "echo", provider: "test", region: "eu" });
    expect((spec.model_settings as Record<string, unknown>).top_p).toBe(0.9);
    expect(spec.name).toBe("Support bot 2");
    expect({ ...spec, name: "Support bot" }).toEqual(STORED);
  });

  it("an untouched draft is clean: no Save-before-publish notice, Publish enabled", async () => {
    await renderEditor();
    expect(screen.queryByText("Save the draft before publishing")).toBeNull();
    expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    expect(screen.getByText("Save the draft before publishing")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Support bot" } });
    expect(screen.queryByText("Save the draft before publishing")).toBeNull();
  });
});

describe("unsaved-changes guard", () => {
  const link = (href: string, attrs: Record<string, string> = {}) => {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = "elsewhere";
    // After the guard's capture listener has looked at the click; jsdom cannot navigate.
    a.addEventListener("click", (event) => event.preventDefault());
    for (const [key, value] of Object.entries(attrs)) a.setAttribute(key, value);
    document.body.appendChild(a);
    return a;
  };
  const dirty = () => fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Edited" } });
  const unload = () => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  };
  afterEach(() => document.querySelectorAll("a[data-guard]").forEach((a) => a.remove()));

  it("a dirty click on the agent tabs or sidebar links asks first; Leave follows, Stay does not", async () => {
    await renderEditor();
    dirty();
    const a = link(routes.agentTraces("a1"), { "data-guard": "" });
    fireEvent.click(a);
    const dialog = await screen.findByRole("dialog", { name: "Leave without saving?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Stay" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Name")).toHaveValue("Edited");
    fireEvent.click(a);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Leave without saving" }));
    expect(nav.push).toHaveBeenCalledWith(routes.agentTraces("a1"));
  });

  it("step changes, ?compare= links and same-page links never prompt, dirty or not", async () => {
    await renderEditor();
    dirty();
    fireEvent.click(screen.getByTestId("stepper-next"));
    fireEvent.click(navButton(/^Tools/));
    fireEvent.click(link(routes.agentEditor("a1", { step: "processors" }), { "data-guard": "" }));
    fireEvent.click(link(routes.agentEditor("a1", { compare: "1..2" }), { "data-guard": "" }));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    expect(unload()).toBe(true);
  });

  it("modified clicks, new-tab links and downloads are not held", async () => {
    await renderEditor();
    dirty();
    const plain = link(routes.agents(), { "data-guard": "" });
    fireEvent.click(plain, { ctrlKey: true });
    fireEvent.click(plain, { metaKey: true });
    fireEvent.click(plain, { button: 1 });
    fireEvent.click(link(routes.agents(), { "data-guard": "", target: "_blank" }));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  it("beforeunload is armed by an edit and disarmed by saving it", async () => {
    await renderEditor();
    expect(unload()).toBe(false);
    dirty();
    expect(unload()).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(unload()).toBe(false));
    fireEvent.click(link(routes.agents(), { "data-guard": "" }));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  it("reverting an edit by hand disarms the guard", async () => {
    await renderEditor();
    dirty();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Support bot" } });
    expect(unload()).toBe(false);
  });
});

describe("versions", () => {
  const FULL = ["Compare with latest", "Compare with draft", "Compare with…", "Test in chat", "Restore this version"];

  it("an older version's row menu offers the five actions; the latest has no latest-compare or restore", async () => {
    await renderEditor();
    expect(await versionActions(1)).toEqual(FULL);
    expect(await versionActions(2)).toEqual(["Compare with draft", "Compare with…", "Test in chat"]);
  });

  it("the Draft row's menu compares with latest, tests in chat and cannot be restored", async () => {
    vi.mocked(api.versions.list).mockResolvedValue([version(2, V1), version(1, V1)]);
    await renderEditor();
    expect(await screen.findByTestId("version-row-draft")).toBeInTheDocument();
    expect(await versionActions("draft")).toEqual(["Compare with latest", "Compare with…", "Test in chat"]);
  });

  it("Compare with draft writes ?compare=1..draft and opens the Sheet with the diff", async () => {
    await renderEditor();
    await chooseVersionAction(1, "Compare with draft");
    expect(lastWrite().params.get("compare")).toBe("1..draft");
    const sheet = await screen.findByRole("dialog", { name: "Compare v1 with Draft" });
    expect(await within(sheet).findByText("model.name")).toBeInTheDocument();
  });

  it("Compare with… lists the other refs and compares with the one chosen", async () => {
    await renderEditor();
    const trigger = await screen.findByRole("button", { name: "Actions for version 1" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    const sub = within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Compare with…" });
    fireEvent.keyDown(sub, { key: "ArrowRight" });
    const items = await screen.findAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual(expect.arrayContaining(["Draft", "v2"]));
    fireEvent.click(items.find((item) => item.textContent === "v2")!);
    await waitFor(() => expect(lastWrite().params.get("compare")).toBe("1..2"));
  });

  it("Test in chat links to the chat on that version, and on the draft", async () => {
    vi.mocked(api.versions.list).mockResolvedValue([version(2, V1), version(1, V1)]);
    await renderEditor();
    for (const ref of [1, "draft"] as const) {
      const trigger = await screen.findByRole("button", { name: ref === "draft" ? "Actions for the draft" : `Actions for version ${ref}` });
      trigger.focus();
      fireEvent.keyDown(trigger, { key: "Enter" });
      const item = within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Test in chat" });
      expect(item).toHaveAttribute("href", routes.agentChat("a1", { version: ref }));
      fireEvent.keyDown(item, { key: "Escape" });
      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    }
  });

  it("Restore from a row asks, restores, and reloads the agent and versions", async () => {
    vi.mocked(restoreVersion).mockResolvedValueOnce({ ...version(3, V1), restored_from: 1 } as never);
    await renderEditor();
    await chooseVersionAction(1, "Restore this version");
    const dialog = await screen.findByRole("dialog", { name: "Restore version 1?" });
    expect(dialog).toHaveTextContent("publishes a new version, 3");
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and publish" }));
    await waitFor(() => expect(restoreVersion).toHaveBeenCalledWith("a1", 1, { discardDraft: false }));
    await waitFor(() => expect(vi.mocked(api.versions.list).mock.calls.length).toBeGreaterThan(1));
  });

  it("the compare Sheet restores a published side other than the latest, then closes", async () => {
    vi.mocked(restoreVersion).mockResolvedValueOnce({ ...version(3, V1), restored_from: 1 } as never);
    nav.search = "compare=1..draft";
    await renderEditor();
    const sheet = await screen.findByRole("dialog", { name: "Compare v1 with Draft" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Restore v1" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore and publish" }));
    await waitFor(() => expect(restoreVersion).toHaveBeenCalledWith("a1", 1, { discardDraft: false }));
    await waitFor(() => expect(lastReplace().params.get("compare")).toBeNull());
  });

  it.each([
    ["2..draft", []],
    ["1..2", ["Restore v1"]],
    ["1..draft", ["Restore v1"]],
  ])("compare %s offers %j", async (value, restores) => {
    nav.search = `compare=${value}`;
    await renderEditor();
    const sheet = await screen.findByRole("dialog", { name: /^Compare/ });
    const names = within(sheet).queryAllByRole("button", { name: /^Restore v/ }).map((button) => button.textContent?.trim());
    expect(names).toEqual(restores);
  });

  it("a restore conflict (409) from the Sheet shows in the dialog and keeps the Sheet open", async () => {
    vi.mocked(restoreVersion).mockRejectedValueOnce(new ApiError(409, "version 1 has the same spec as the latest version 2"));
    nav.search = "compare=1..2";
    await renderEditor();
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Compare v1 with v2" })).getByRole("button", { name: "Restore v1" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore and publish" }));
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("same spec as the latest version 2");
    expect(screen.getByRole("dialog", { name: "Restore version 1?" })).toBeInTheDocument();
  });

  it("with unsaved edits the restore dialog warns they will be lost", async () => {
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Edited" } });
    await chooseVersionAction(1, "Restore this version");
    expect(await screen.findByRole("dialog", { name: "Restore version 1?" })).toHaveTextContent("unsaved edits in the editor will be lost");
  });

  it("no versions: the empty state replaces the list and Compare with latest is absent", async () => {
    vi.mocked(api.versions.list).mockResolvedValue([]);
    await renderEditor();
    expect(await screen.findByTestId("agent-versions-empty")).toHaveTextContent("Publish the draft to create v1");
    expect(screen.queryByTestId("agent-versions")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Compare with latest/ })).toBeNull();
  });
});

describe("Config panel links open the right step", () => {
  const LINKS: [string, string, string][] = [
    ["Edit model", "identity", "Identity and model"],
    ["Edit instructions", "instructions", "Instructions"],
    ["Edit tools", "tools", "Tools"],
    ["Edit request context fields", "request-context", "Request context"],
    ["Edit processors", "processors", "Processors"],
  ];
  const stepOf = (href: string) => new URL(href, "http://localhost").searchParams.get("step");
  const searchOf = (href: string) => new URL(href, "http://localhost").search.slice(1);
  const agent = agentWith(DRAFT) as never;
  const published = [version(2, DRAFT), version(1, V1)] as never;

  it.each(LINKS)("%s links to ?step=%s and the editor opens %s", async (name, id, label) => {
    const config = render(<ConfigPanel agent={agent} versions={published} />);
    const href = screen.getByRole("link", { name }).getAttribute("href")!;
    expect(href.split("?")[0]).toBe(routes.agentEditor("a1"));
    expect(stepOf(href)).toBe(id);
    expect(href).toBe(routes.agentEditor("a1", { step: id }));
    config.unmount();
    nav.search = searchOf(href);
    await renderEditor();
    expect(navButton(new RegExp(`^${label}`))).toHaveAttribute("aria-current", "step");
    expect(region(label)).toBeVisible();
  });

  it("the Version section links to the editor without a step", () => {
    render(<ConfigPanel agent={agent} versions={published} />);
    expect(screen.getByRole("link", { name: "Edit version" })).toHaveAttribute("href", routes.agentEditor("a1"));
  });

  it("the chat context popover's 'Fields are defined in the editor' link opens Request context", async () => {
    // The popover builds this href (RequestContextPopover.test asserts the link itself).
    const href = routes.agentEditor("a1", { step: "request-context" });
    nav.search = searchOf(href);
    await renderEditor();
    expect(navButton(/^Request context/)).toHaveAttribute("aria-current", "step");
    expect(region("Request context")).toBeVisible();
  });

  it("following a link while the editor is open switches the step (the URL is the source)", async () => {
    const view = await renderEditor();
    expect(navButton(/^Identity and model/)).toHaveAttribute("aria-current", "step");
    nav.search = "step=processors";
    view.rerender(<EditorTab agentId="a1" />);
    await waitFor(() => expect(navButton(/^Processors/)).toHaveAttribute("aria-current", "step"));
  });
});

describe("layouts", () => {
  function Page({ config }: { config?: boolean }) {
    return (
      <SidePanelProvider>
        {config ? <SidePanel title="Config" defaultOpen>config</SidePanel> : null}
        <EditorTab agentId="a1" />
      </SidePanelProvider>
    );
  }
  let media: ReturnType<typeof mockMatchMedia>;
  afterEach(() => media?.restore());
  async function at(width: number, config = false) {
    restoreWidth();
    restoreWidth = setWindowWidth(width);
    media = mockMatchMedia(width >= 1024);
    await renderEditor(<Page config={config} />);
  }

  it("1440 px: columns, nav and the versions column", async () => {
    await at(1440);
    expect(stepper()).toHaveAttribute("data-layout", "columns");
    expect(screen.getByTestId("stepper-nav")).toBeInTheDocument();
    expect(screen.getByTestId("versions-column")).toBeInTheDocument();
    expect(screen.queryByTestId("versions-sheet-toggle")).toBeNull();
    expect(screen.queryByTestId("stepper-select")).toBeNull();
  });

  it("1440 px with Config open: nav and step, versions in the Sheet", async () => {
    await at(1440, true);
    expect(stepper()).toHaveAttribute("data-layout", "nav");
    expect(screen.getByTestId("stepper-nav")).toBeInTheDocument();
    expect(screen.queryByTestId("versions-column")).toBeNull();
    fireEvent.click(screen.getByTestId("versions-sheet-toggle"));
    const sheet = await screen.findByTestId("versions-sheet");
    expect(within(sheet).getByTestId("agent-versions")).toBeInTheDocument();
  });

  it("1024 px: nav and step, versions in the Sheet", async () => {
    await at(1024);
    expect(stepper()).toHaveAttribute("data-layout", "nav");
    expect(screen.getByTestId("versions-sheet-toggle")).toBeInTheDocument();
    expect(screen.queryByTestId("versions-column")).toBeNull();
  });

  it("375 px: the step select, versions in the Sheet, no nav", async () => {
    await at(375);
    expect(stepper()).toHaveAttribute("data-layout", "compact");
    expect(screen.getByTestId("stepper-select")).toBeInTheDocument();
    expect(screen.queryByTestId("stepper-nav")).toBeNull();
    expect(screen.getByTestId("versions-sheet-toggle")).toBeInTheDocument();
  });

  it("follows a window resize", async () => {
    await at(1440);
    expect(stepper()).toHaveAttribute("data-layout", "columns");
    restoreWidth();
    restoreWidth = setWindowWidth(375);
    act(() => void window.dispatchEvent(new Event("resize")));
    await waitFor(() => expect(stepper()).toHaveAttribute("data-layout", "compact"));
  });

  it("at 375 px nothing in the stepper has a fixed width wider than the window", async () => {
    await at(375);
    const offenders: string[] = [];
    for (const element of [stepper(), ...Array.from(stepper().querySelectorAll("*"))]) {
      const classes = (element.getAttribute("class") ?? "").split(/\s+/);
      for (const name of classes) {
        const match = /^(?:min-w|w)-\[(\d+)px\]$/.exec(name);
        if (match && Number(match[1]) > 375) offenders.push(name);
        if (/^(?:md|lg|xl):grid-cols-\[/.test(name) && !classes.includes("grid-cols-1")) offenders.push(`${name} without grid-cols-1`);
      }
    }
    expect(offenders).toEqual([]);
    const root = stepper();
    expect(root.className).toContain("min-w-0");
    expect(within(root).getByTestId("stepper-select").className).toContain("w-full");
    expect(root.querySelector(":scope > div.grid")?.className).toContain("grid-cols-1");
  });
});

describe("registry contract", () => {
  function KnowledgeStep({ spec, onSpecChange }: StepProps) {
    const sources = (spec as unknown as { knowledge?: { sources?: string } }).knowledge?.sources ?? "";
    return (
      <label>
        Knowledge sources
        <input value={sources} onChange={(event) => onSpecChange({ knowledge: event.target.value ? { sources: event.target.value } : undefined })} />
      </label>
    );
  }
  const knowledgeStep: EditorStepDefinition = {
    id: "knowledge",
    label: "Knowledge",
    Component: KnowledgeStep,
    status: (spec) => {
      const sources = (spec as unknown as { knowledge?: { sources?: string } }).knowledge?.sources;
      return sources ? { text: `Sources: ${sources}`, valid: true } : { text: "Not set", valid: false };
    },
    ownsError: (message) => message.startsWith("knowledge"),
  };
  // The five built-in steps plus the fake: RM.17's real Knowledge and Memory steps are left out, so
  // the fake's id and the unknown `memory` key below stay the contract's own.
  const STEPS = [...EDITOR_STEPS.filter((step) => (CORE_STEP_IDS as readonly string[]).includes(step.id)), knowledgeStep];
  const STORED = { ...DRAFT, model_settings: { temperature: null, max_tokens: null }, memory: { on: true } } as unknown as AgentSpec;

  function Host({ onSubmit, serverError = null }: { onSubmit: (spec: AgentSpec) => Promise<void>; serverError?: string | null }) {
    const [step, setStep] = React.useState("identity");
    return <AgentEditor initialData={STORED} onSubmit={onSubmit} submitLabel="Save draft" serverError={serverError} steps={STEPS} stepper={{ activeStep: step, onStepChange: setStep, layout: "nav" }} />;
  }

  it("a fake sixth step appears in the nav, in Next, with its status, with no other change", async () => {
    render(<Host onSubmit={async () => undefined} />);
    expect(within(stepNav()).getAllByRole("button")).toHaveLength(6);
    expect(navButton(/^Knowledge/)).toHaveTextContent("Not set");
    expect(navButton(/^Knowledge/)).toHaveAccessibleName(/needs attention/);
    fireEvent.click(navButton(/^Processors/));
    expect(screen.getByTestId("stepper-next")).toHaveAccessibleName("Next step: Knowledge");
    fireEvent.click(screen.getByTestId("stepper-next"));
    expect(region("Knowledge")).toBeVisible();
    expect(screen.queryByTestId("stepper-next")).toBeNull();
    expect(screen.getByTestId("stepper-previous")).toHaveAccessibleName("Previous step: Processors");
    fireEvent.change(screen.getByLabelText("Knowledge sources"), { target: { value: "docs" } });
    expect(navButton(/^Knowledge/)).toHaveTextContent("Sources: docs");
    expect(navButton(/^Knowledge/)).toHaveAccessibleName(/complete/);
  });

  it("its edits save as a top-level key; the unknown memory key survives; clearing removes it", async () => {
    const onSubmit = vi.fn(async (_spec: AgentSpec) => undefined);
    render(<Host onSubmit={onSubmit} />);
    fireEvent.click(navButton(/^Knowledge/));
    fireEvent.change(screen.getByLabelText("Knowledge sources"), { target: { value: "docs" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ knowledge: { sources: "docs" }, memory: { on: true } });
    fireEvent.change(screen.getByLabelText("Knowledge sources"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
    expect(JSON.stringify(onSubmit.mock.calls[1][0])).toBe(JSON.stringify(STORED));
  });

  it("its ownsError opens it on a save error", async () => {
    render(<Host onSubmit={async () => undefined} serverError="knowledge.sources: unreachable" />);
    await waitFor(() => expect(region("Knowledge")).toBeVisible());
    expect(navButton(/^Knowledge/)).toHaveTextContent("Has an error");
  });
});

describe("accessibility", () => {
  const headings = () => Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6")).map((h) => h.tagName);
  function Page() {
    return (
      <SidePanelProvider>
        <main>
          <h1>Support bot</h1>
          <EditorTab agentId="a1" />
        </main>
      </SidePanelProvider>
    );
  }
  async function settle() {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
  afterEach(() => {
    document.documentElement.classList.remove("dark");
    document.documentElement.removeAttribute("data-theme");
  });

  it.each(["light", "dark"])("has no axe violations in the %s theme and one h1 without skipped levels", async (theme) => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.setAttribute("data-theme", theme);
    await renderEditor(<Page />);
    await settle();
    await expectNoAxeViolations(document.body);
    const tags = headings();
    expect(tags.filter((tag) => tag === "H1")).toHaveLength(1);
    const levels = tags.map((tag) => Number(tag.slice(1)));
    expect(levels.every((level, index) => level <= (levels[index - 1] ?? 0) + 1)).toBe(true);
  }, AXE_TIMEOUT_MS);

  it.each(["light", "dark"])("the compare Sheet and the versions Sheet have no axe violations in the %s theme", async (theme) => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.setAttribute("data-theme", theme);
    nav.search = "compare=1..2";
    const media = mockMatchMedia(true);
    restoreWidth();
    restoreWidth = setWindowWidth(1024);
    const view = await renderEditor(<Page />);
    const sheet = await screen.findByTestId("compare-sheet");
    await within(sheet).findByText("model.name");
    await expectNoAxeViolations(document.body);
    view.unmount();
    nav.search = "";
    await renderEditor(<Page />);
    fireEvent.click(screen.getByTestId("versions-sheet-toggle"));
    await within(await screen.findByTestId("versions-sheet")).findByTestId("agent-versions");
    await expectNoAxeViolations(document.body);
    media.restore();
  }, AXE_TIMEOUT_MS);
});
