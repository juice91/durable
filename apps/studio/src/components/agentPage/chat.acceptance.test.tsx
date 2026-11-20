// SL.10 acceptance: the chat rework (landing and dock, scroll-to-bottom, chips, request context
// presets and popover, tool-call cards, redirects, Open trace, tokens, axe). Complements
// chatComposer.test.tsx and the migrated P0.16 / P0.19 suites; nothing here repeats those.
import React from "react";
import "@testing-library/jest-dom";
import fs from "fs";
import path from "path";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Like Next's router: replace() rewrites the address bar and tells subscribers. redirect() throws.
const nav = vi.hoisted(() => ({ path: "/agents/a1/chat", listeners: new Set<() => void>() }));
vi.mock("next/navigation", async () => {
  const R = await import("react");
  const notify = () => nav.listeners.forEach((l) => l());
  const replace = (url: string) => {
    window.history.replaceState({}, "", url);
    notify();
  };
  // push and back keep a small history stack, so a Sheet opened with push closes with back.
  const stack: string[] = [];
  const push = (url: string) => {
    stack.push(window.location.pathname + window.location.search);
    replace(url);
  };
  const back = () => {
    const prev = stack.pop();
    if (prev !== undefined) replace(prev);
  };
  return {
    usePathname: () => nav.path,
    useParams: () => ({}),
    useRouter: () => ({ push, replace, back, prefetch: vi.fn() }),
    useSearchParams: () => {
      const s = R.useSyncExternalStore(
        (cb) => (nav.listeners.add(cb), () => nav.listeners.delete(cb)),
        () => window.location.search,
        () => "",
      );
      return R.useMemo(() => new URLSearchParams(s), [s]);
    },
    redirect: (url: string) => {
      throw Object.assign(new Error("NEXT_REDIRECT"), { redirectTo: url });
    },
  };
});
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
vi.mock("@/lib/metrics", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/metrics")>()), fetchModels: vi.fn() }));
vi.mock("@/components/chat/turnApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/chat/turnApi")>()),
  startTurnWithOptions: vi.fn(),
}));
vi.mock("@/lib/requestContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/requestContext")>()),
  getRequestContext: vi.fn(),
  validateRequestContext: vi.fn(),
}));
vi.mock("@/lib/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/runs")>()),
  listEvents: vi.fn(),
  subscribeRun: vi.fn(),
  getRun: vi.fn(),
}));
vi.mock("@/lib/traces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/traces")>()),
  listTraceRuns: vi.fn(),
  getTrace: vi.fn(),
}));

import { api, type Agent, type AgentSpec, type AgentVersion } from "@/lib/api";
import { createConversation, getConversation, listConversations } from "@/lib/conversations";
import { fetchModels } from "@/lib/metrics";
import {
  MIGRATED_PRESET_NAME, PRESETS_PREFIX, deletePreset, getRequestContext, latestPreset, listPresets, presetNameError,
  presetsKey, saveContext, savePreset, validateRequestContext, type RequestContextInfo,
} from "@/lib/requestContext";
import { getRun, listEvents, subscribeRun, type RunEvent, type RunEventData } from "@/lib/runs";
import { getTrace, listTraceRuns, type Trace, type TraceRun } from "@/lib/traces";
import { TurnStartError, startTurnWithOptions } from "@/components/chat/turnApi";
import { clearBrowserData, BROWSER_DATA } from "@/components/settings/browserData";
import { CONTEXT_SHEET_QUERY } from "@/components/requestContext/RequestContextPopover";
import { FrameHarness, mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH, type MatchMediaControl } from "@/components/frame/frameTesting";
import { routes } from "@/lib/routes";
import { legacyTarget } from "@/lib/routes/legacy";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { AgentLayout } from "./AgentLayout";
import { ChatTab } from "./ChatTab";
import { TracesTab } from "./TracesTab";
import { findSelectOption, optionsOf } from "@/test/select";

const spec = (instructions: string, extra: object = {}): AgentSpec =>
  ({ spec_version: 1, name: "Support bot", instructions, model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [], ...extra }) as AgentSpec;
const AGENT: Agent = { id: "a1", name: "Support bot", draft_spec: spec("draft"), latest_version: 2 };
const VERSIONS: AgentVersion[] = [1, 2].map((version) => ({
  agent_id: "a1", version, spec: spec(`v${version}`), spec_hash: `h${version}`, created_at: "2026-11-14T00:00:00Z", created_by: "dev",
}));
const TIER: RequestContextInfo = {
  agent_id: "a1", version: 2, draft: false,
  fields: [{ name: "tier", type: "enum", required: true, description: "", max_length: null, values: ["gold", "silver"] }],
  schema: { type: "object", required: ["tier"], properties: { tier: { type: "string", enum: ["gold", "silver"] } } },
};
const ev = (seq: number, type: RunEvent["type"], data: object, ts = "2026-11-14T10:00:00Z"): RunEvent => ({ seq, type, data: data as RunEventData, ts });
const turnRun = (run_id: string, turn: number) => ({
  run_id, agent_id: "a1", version: 2, status: "completed", display_status: "completed", input: `q${turn}`, output: null, error: null,
  heartbeat_at: null, created_at: "", updated_at: "", trace_id: null, conversation_id: "c1", turn,
});
function mockConversation(runIds: string[], events: Record<string, RunEvent[]> = {}) {
  vi.mocked(getConversation).mockResolvedValue({
    id: "c1", agent_id: "a1", title: "T", turn_count: runIds.length, created_at: "", updated_at: "", runs: runIds.map((id, i) => turnRun(id, i + 1)),
  } as never);
  vi.mocked(listEvents).mockImplementation(async (id) => events[id] ?? [ev(0, "run_started", { input: `q ${id}` }), ev(1, "model_message", { model_request: 0, text: `answer ${id}`, is_final: true }), ev(2, "run_completed", {})]);
}
const search = () => new URLSearchParams(window.location.search);
const box = () => screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
const ctxButton = () => screen.getByRole("button", { name: /Request context/ });
const choose = (name: string, value: string) => fireEvent.change(screen.getByRole("combobox", { name }), { target: { value } });
let media: MatchMediaControl | undefined;
let restoreWidth: () => void = () => {};

beforeEach(() => {
  vi.clearAllMocks();
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState({}, "", routes.agentChat("a1"));
  nav.path = routes.agentChat("a1");
  vi.mocked(api.agents.get).mockResolvedValue(AGENT);
  vi.mocked(api.agents.list).mockResolvedValue([AGENT]);
  vi.mocked(api.versions.list).mockResolvedValue(VERSIONS);
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null } as never);
  vi.mocked(fetchModels).mockResolvedValue([
    { id: "openai:gpt-4.1-mini", provider: "openai", name: "gpt-4.1-mini", wildcard: false, source: "allowlist", description: null },
    { id: "openai:gpt-4.1", provider: "openai", name: "gpt-4.1", wildcard: false, source: "allowlist", description: null },
  ]);
  vi.mocked(getRequestContext).mockResolvedValue(TIER);
  vi.mocked(validateRequestContext).mockImplementation(async (_a, value) => value);
  vi.mocked(subscribeRun).mockReturnValue(() => undefined);
  vi.mocked(listEvents).mockResolvedValue([]);
  vi.mocked(createConversation).mockImplementation(async (agentId, title, id) => ({ id: id ?? "c1", agent_id: agentId, title, turn_count: 0, created_at: "", updated_at: "" }) as never);
  vi.mocked(startTurnWithOptions).mockImplementation(async (conversationId, runId) => ({
    run_id: runId, status: "queued", display_status: "queued", agent_id: "a1", version: 2, draft: false,
    model_id: "openai:gpt-4.1-mini", model_overridden: false, conversation_id: conversationId, turn: 1,
  }) as never);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  media?.restore();
  media = undefined;
  restoreWidth();
  document.documentElement.classList.remove("dark");
});

async function renderChat(props: { conversationId?: string | null } = {}) {
  const view = render(<ChatTab agentId="a1" conversationId={props.conversationId ?? null} />);
  await waitFor(() => expect(ctxButton()).toHaveAccessibleName(/of 1 set/));
  return view;
}

describe("landing to dock", () => {
  it("keeps the very same composer element, textarea and focus through the first send", async () => {
    await renderChat();
    const dock = screen.getByTestId("chat-composer-dock");
    const composer = screen.getByTestId("chat-composer");
    const input = box();
    input.focus();
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: "Hello" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("chat-composer-dock")).toHaveAttribute("data-docked", "true"));
    expect(startTurnWithOptions).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("chat-composer-dock")).toBe(dock);
    expect(screen.getByTestId("chat-composer")).toBe(composer);
    expect(box()).toBe(input);
    expect(input).toHaveValue("");
    expect(document.activeElement).toBe(input);
    expect(dock).toContainElement(composer);
  });

  it("starts docked for a conversation that already has turns", async () => {
    mockConversation(["r1"]);
    await renderChat({ conversationId: "c1" });
    await screen.findByText("answer r1");
    expect(screen.getByTestId("chat-composer-dock")).toHaveAttribute("data-docked", "true");
    expect(screen.queryByTestId("chat-landing")).toBeNull();
  });

  it("New conversation returns to the landing with the composer still mounted", async () => {
    mockConversation(["r1"]);
    window.history.replaceState({}, "", routes.agentChat("a1", { conversation: "c1" }));
    await renderChat({ conversationId: "c1" });
    await screen.findByText("answer r1");
    const composer = screen.getByTestId("chat-composer");
    fireEvent.click(within(screen.getByTestId("threads-panel")).getByRole("button", { name: /New conversation/ }));
    expect(await screen.findByTestId("chat-landing")).toBeInTheDocument();
    expect(screen.getByTestId("chat-composer-dock")).toHaveAttribute("data-docked", "false");
    expect(screen.getByTestId("chat-composer")).toBe(composer);
    expect(search().get("conversation")).toBeNull();
  });
});

describe("scroll to bottom", () => {
  it("appears once the transcript is scrolled up and scrolls the viewport to its end", async () => {
    mockConversation(["r1", "r2", "r3"]);
    await renderChat({ conversationId: "c1" });
    await screen.findByText("answer r3");
    const viewport = screen.getByTestId("chat-transcript").parentElement as HTMLElement;
    const state = { top: 0 };
    Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(viewport, "scrollTop", { configurable: true, get: () => state.top, set: (v: number) => void (state.top = v) });
    const scrollTo = vi.fn((arg: ScrollToOptions | number) => void (state.top = typeof arg === "number" ? arg : (arg.top ?? 0)));
    viewport.scrollTo = scrollTo as unknown as typeof viewport.scrollTo;
    const button = screen.getByTestId("chat-scroll-to-bottom");
    // At the bottom: hidden and inert. Scrolled up: shown.
    state.top = 1600;
    fireEvent.scroll(viewport);
    await waitFor(() => expect(button).toBeDisabled());
    state.top = 0;
    fireEvent.scroll(viewport);
    await waitFor(() => expect(button).toBeEnabled());
    expect(button).toHaveAccessibleName("Scroll to bottom");
    fireEvent.click(button);
    expect(state.top).toBeGreaterThanOrEqual(1600);
  });
});

describe("chips and the URL (as RunOptionsBar did, P0.19)", () => {
  async function ready() {
    await renderChat();
    await findSelectOption("openai:gpt-4.1");
  }

  it("write version and model to the query with replaceState, keep other parameters, and drop defaults", async () => {
    window.history.replaceState({}, "", `${routes.agentChat("a1", { conversation: "c9" })}?keep=1`);
    const entries = window.history.length;
    await ready();
    choose("Version", "v1");
    expect(search().get("version")).toBe("1");
    choose("Model", "openai:gpt-4.1");
    expect(search().get("model")).toBe("openai:gpt-4.1");
    choose("Version", "draft");
    expect(search().get("version")).toBe("draft");
    expect(search().get("model")).toBe("openai:gpt-4.1");
    choose("Version", "latest");
    expect(search().has("version")).toBe(false);
    choose("Model", "");
    expect(search().has("model")).toBe(false);
    expect(search().get("keep")).toBe("1");
    expect(window.location.pathname).toBe(routes.agentChat("a1"));
    expect(window.history.length).toBe(entries);
  });

  it("read version and model back from the URL into the chips", async () => {
    window.history.replaceState({}, "", routes.agentChat("a1", { version: 1, model: "openai:gpt-4.1" }));
    await ready();
    expect(screen.getByRole("combobox", { name: "Version" })).toHaveValue("v1");
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("openai:gpt-4.1");
    expect(screen.getByTestId("composer-chip-version")).toHaveTextContent("v1");
    expect(screen.getByTestId("composer-chip-model")).toHaveTextContent("gpt-4.1");
  });

  it("lists Draft first (as the first option value) when it differs, and last otherwise", async () => {
    await ready();
    const values = () => optionsOf(screen.getByRole("combobox", { name: "Version" })).map((o) => o.value);
    expect(values()).toEqual(["draft", "latest", "v2", "v1"]);
    cleanup();
    vi.mocked(api.agents.get).mockResolvedValue({ ...AGENT, draft_spec: spec("v2") });
    await ready();
    await waitFor(() => expect(values()).toEqual(["latest", "v2", "v1", "draft"]));
  });

  it("fit a phone: the row wraps, each chip shrinks and truncates, nothing forces a width", async () => {
    await ready();
    const row = screen.getByTestId("run-options");
    expect(row).toHaveClass("flex", "flex-wrap", "min-w-0");
    for (const id of ["composer-chip-version", "composer-chip-model"]) {
      const chip = screen.getByTestId(id);
      expect(chip).toHaveClass("min-w-0", "shrink", "max-w-[11rem]");
      expect(chip.querySelector(".truncate")).not.toBeNull();
      expect(row).toContainElement(chip);
    }
    expect(screen.getByTestId("chat-composer")).toHaveClass("min-w-0");
    expect(screen.getByTestId("chat-composer-dock")).toHaveClass("min-w-0", "max-w-3xl", "w-full");
    expect(row.className).not.toMatch(/(^|\s)w-\[|(^|\s)w-\d|min-w-\[|whitespace-nowrap/);
  });

  it("show the output-checked hint as screen-reader text below sm", async () => {
    vi.mocked(api.versions.list).mockResolvedValue([VERSIONS[0], { ...VERSIONS[1], spec: spec("v2", { output_processors: [{ type: "regex_block", patterns: ["x"] }] }) }]);
    await ready();
    const hint = await screen.findByTestId("composer-output-checked");
    expect(hint.querySelector(".hidden.sm\\:inline")).toHaveTextContent("Output checked before display");
    expect(hint.querySelector(".sr-only")).toHaveTextContent("Output checked before display");
  });
});

describe("request context presets", () => {
  async function openPopover() {
    await renderChat();
    fireEvent.click(ctxButton());
    return screen.findByRole("dialog", { name: "Request context" });
  }
  async function saveAs(name: string, tier = "gold") {
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: tier } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: "Save as…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Preset name" }), { target: { value: name } });
    fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
  }

  it("round-trips through the popover: save, load, update, delete", async () => {
    await openPopover();
    await saveAs("Gold");
    await saveAs("Silver", "silver");
    expect(listPresets("a1").map((p) => [p.name, p.value])).toEqual([["Gold", { tier: "gold" }], ["Silver", { tier: "silver" }]]);
    const select = screen.getByRole("combobox", { name: "Preset" });
    fireEvent.change(select, { target: { value: "Gold" } });
    expect(screen.getByLabelText(/Tier/)).toHaveValue("gold");
    fireEvent.change(screen.getByLabelText(/Tier/), { target: { value: "silver" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Update" })).toBeEnabled(), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(listPresets("a1").find((p) => p.name === "Gold")?.value).toEqual({ tier: "silver" });
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(listPresets("a1").map((p) => p.name)).toEqual(["Silver"]);
    expect(screen.queryByRole("option", { name: "Gold" })).toBeNull();
  });

  it("saving under an existing name replaces it instead of adding a second one", async () => {
    await openPopover();
    await saveAs("Gold");
    await saveAs("  Gold ", "silver");
    expect(listPresets("a1")).toEqual([expect.objectContaining({ name: "Gold", value: { tier: "silver" } })]);
  });

  it("validates names: 1 to 60 characters after trimming, refused inline in the dialog and in the store", async () => {
    expect(presetNameError("")).toBe("Enter a name");
    expect(presetNameError("   ")).toBe("Enter a name");
    expect(presetNameError("a")).toBeNull();
    expect(presetNameError(` ${"x".repeat(60)} `)).toBeNull();
    expect(presetNameError("x".repeat(61))).toMatch(/at most 60/);
    expect(savePreset("a1", "x".repeat(61), { tier: "gold" }, null)).toBeNull();
    expect(savePreset("a1", " ", { tier: "gold" }, null)).toBeNull();
    expect(listPresets("a1")).toEqual([]);
    await openPopover();
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: "Save as…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Preset name" }), { target: { value: "x".repeat(61) } });
    fireEvent.click(screen.getByRole("button", { name: "Save preset" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/at most 60/);
    expect(listPresets("a1")).toEqual([]);
  });
});

describe("preset store: newest first, migration, Settings", () => {
  afterEach(() => vi.useRealTimers());

  it("preselects the most recently saved preset, and an Update makes it the newest", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-14T09:00:00Z"));
    savePreset("a1", "Alpha", { tier: "gold" }, null);
    vi.setSystemTime(new Date("2026-11-14T10:00:00Z"));
    savePreset("a1", "Zulu", { tier: "silver" }, null);
    expect(latestPreset("a1")?.name).toBe("Zulu");
    vi.setSystemTime(new Date("2026-11-14T11:00:00Z"));
    savePreset("a1", "Alpha", { tier: "gold" }, null);
    expect(latestPreset("a1")?.name).toBe("Alpha");
    // Listed by name, preselected by recency.
    expect(listPresets("a1").map((p) => p.name)).toEqual(["Alpha", "Zulu"]);
    vi.useRealTimers();
    await renderChat();
    fireEvent.click(ctxButton());
    expect(await screen.findByRole("combobox", { name: "Preset" })).toHaveValue("Alpha");
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Using preset “Alpha”.");
  });

  it("deleting the newest hands the preselection to the next newest; none left means none", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-14T09:00:00Z"));
    savePreset("a1", "Old", { tier: "gold" }, 1);
    vi.setSystemTime(new Date("2026-11-14T10:00:00Z"));
    savePreset("a1", "New", { tier: "silver" }, 2);
    expect(deletePreset("a1", "New")).toBe(true);
    expect(latestPreset("a1")?.name).toBe("Old");
    expect(deletePreset("a1", "Old")).toBe(true);
    expect(deletePreset("a1", "Missing")).toBe(true);
    expect(latestPreset("a1")).toBeNull();
  });

  it("migrates the old key once: one preset named Saved, same value and version, old key kept", () => {
    const old = saveContext("a1", { tier: "silver" }, 2);
    expect(old).not.toBeNull();
    expect(listPresets("a1")).toEqual([expect.objectContaining({ name: MIGRATED_PRESET_NAME, value: { tier: "silver" }, version: 2 })]);
    const stored = window.localStorage.getItem(presetsKey("a1"));
    expect(JSON.parse(stored!)).toMatchObject({ migrated: true });
    for (let i = 0; i < 3; i += 1) listPresets("a1");
    latestPreset("a1");
    expect(window.localStorage.getItem(presetsKey("a1"))).toBe(stored);
    expect(Object.keys(window.localStorage).filter((k) => k.startsWith("eap.requestContext.v1:"))).toHaveLength(1);
  });

  it("does not bring Saved back after it is deleted, nor after a new old value appears", () => {
    saveContext("a1", { tier: "silver" }, 2);
    expect(listPresets("a1")).toHaveLength(1);
    expect(deletePreset("a1", MIGRATED_PRESET_NAME)).toBe(true);
    expect(listPresets("a1")).toEqual([]);
    expect(latestPreset("a1")).toBeNull();
    saveContext("a1", { tier: "gold" }, 1);
    expect(listPresets("a1")).toEqual([]);
  });

  it("an agent without an old value stores nothing on read; another agent's old value is not imported", () => {
    saveContext("a2", { tier: "gold" }, null);
    expect(listPresets("a1")).toEqual([]);
    expect(window.localStorage.getItem(presetsKey("a1"))).toBeNull();
    expect(listPresets("a2").map((p) => p.name)).toEqual(["Saved"]);
  });

  it("a failing write retries on the next read and never duplicates", () => {
    saveContext("a1", { tier: "silver" }, 2);
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(listPresets("a1").map((p) => p.name)).toEqual(["Saved"]);
    expect(listPresets("a1").map((p) => p.name)).toEqual(["Saved"]);
    expect(latestPreset("a1")?.name).toBe("Saved");
    expect(window.localStorage.getItem(presetsKey("a1"))).toBeNull();
    expect(savePreset("a1", "Other", { tier: "gold" }, null)).toBeNull();
    set.mockRestore();
    expect(listPresets("a1").map((p) => p.name)).toEqual(["Saved"]);
    expect(JSON.parse(window.localStorage.getItem(presetsKey("a1"))!)).toMatchObject({ migrated: true, presets: [expect.objectContaining({ name: "Saved" })] });
    expect(listPresets("a1")).toHaveLength(1);
  });

  it("a preset already named Saved is not duplicated by the import", () => {
    window.localStorage.setItem(presetsKey("a1"), JSON.stringify({ presets: [{ name: "Saved", value: { tier: "gold" }, version: null, saved_at: "2026-11-14T00:00:00Z" }] }));
    saveContext("a1", { tier: "silver" }, 2);
    expect(listPresets("a1")).toEqual([expect.objectContaining({ name: "Saved", value: { tier: "gold" } })]);
  });

  it("Settings clears the presets and the old key together, and leaves the rest", () => {
    saveContext("a1", { tier: "silver" }, 2);
    savePreset("a1", "Mine", { tier: "gold" }, null);
    window.localStorage.setItem("studio.theme", "dark");
    const entry = BROWSER_DATA.find((e) => e.id === "request-context");
    expect(entry?.prefixes).toEqual(expect.arrayContaining([PRESETS_PREFIX, "eap.requestContext.v1:"]));
    expect(entry?.clearGroup).toBe("presets");
    const result = clearBrowserData("presets");
    expect(result).not.toBeNull();
    expect(Object.keys(window.localStorage).filter((k) => k.includes("equestContext"))).toEqual([]);
    expect(window.localStorage.getItem("studio.theme")).toBe("dark");
    expect(listPresets("a1")).toEqual([]);
  });
});

describe("request context popover", () => {
  const refuse = () =>
    vi.mocked(startTurnWithOptions).mockRejectedValue(
      new TurnStartError(422, [{ loc: ["body", "request_context", "tier"], msg: "tier is not allowed", type: "value_error" }]),
    );

  it("opens by itself on a 422 about the request context, with the refusal inside, and keeps the message", async () => {
    refuse();
    await renderChat();
    expect(screen.queryByRole("dialog", { name: "Request context" })).toBeNull();
    fireEvent.change(box(), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    const dialog = await screen.findByRole("dialog", { name: "Request context" });
    expect(ctxButton()).toHaveAttribute("aria-expanded", "true");
    expect(dialog).toHaveTextContent("tier is not allowed");
    expect(screen.getByTestId("chat-composer-dock")).toHaveAttribute("data-docked", "false");
    expect(screen.getByTestId("unsent-message")).toBeInTheDocument();
  });

  it("a 422 about something else does not open it", async () => {
    vi.mocked(startTurnWithOptions).mockRejectedValue(new TurnStartError(422, "model 'x:y' is not allowed on this platform"));
    await renderChat();
    fireEvent.change(box(), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText(/is not allowed on this platform/);
    expect(screen.queryByRole("dialog", { name: "Request context" })).toBeNull();
  });

  it("opens from ?context=open on a docked conversation too, and the flag is read once", async () => {
    mockConversation(["r1"]);
    window.history.replaceState({}, "", routes.agentChat("a1", { conversation: "c1", context: "open" }));
    await renderChat({ conversationId: "c1" });
    const dialog = await screen.findByRole("dialog", { name: "Request context" });
    expect(dialog).toHaveAttribute("id", "playground-context-panel");
    expect(search().has("context")).toBe(false);
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Request context" })).toBeNull());
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("dialog", { name: "Request context" })).toBeNull();
  });

  it("is a dialog of 20rem (480px from md) on the wide layout, anchored to the button", async () => {
    await renderChat();
    fireEvent.click(ctxButton());
    const popover = await screen.findByTestId("request-context-popover");
    expect(popover).toHaveClass("w-80", "md:w-[480px]");
    expect(popover).toHaveAttribute("role", "dialog");
    expect(popover).toHaveAccessibleName("Request context");
    expect(screen.queryByTestId("request-context-sheet")).toBeNull();
  });

  it.each([["Escape"], ["the button"]])("returns focus to the button when closed with %s", async (how) => {
    await renderChat();
    ctxButton().focus();
    fireEvent.click(ctxButton());
    const dialog = await screen.findByRole("dialog", { name: "Request context" });
    if (how === "Escape") fireEvent.keyDown(dialog, { key: "Escape" });
    else fireEvent.click(ctxButton());
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Request context" })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(ctxButton()));
  });

  it("is a Sheet below 640 px, titled, closed by Escape with focus back on the button", async () => {
    const original = window.matchMedia;
    const wide = mockMatchMedia(true);
    const base = window.matchMedia;
    window.matchMedia = ((q: string) => (q === CONTEXT_SHEET_QUERY ? { ...base(q), matches: true } : base(q))) as typeof window.matchMedia;
    try {
      await renderChat();
      ctxButton().focus();
      fireEvent.click(ctxButton());
      const sheet = await screen.findByTestId("request-context-sheet");
      expect(sheet).toHaveAttribute("role", "dialog");
      expect(within(sheet).getByRole("heading", { name: "Request context" })).toBeInTheDocument();
      expect(screen.queryByTestId("request-context-popover")).toBeNull();
      expect(within(sheet).getByTestId("context-presets")).toBeInTheDocument();
      fireEvent.keyDown(sheet, { key: "Escape" });
      await waitFor(() => expect(screen.queryByTestId("request-context-sheet")).toBeNull());
      await waitFor(() => expect(document.activeElement).toBe(ctxButton()));
    } finally {
      window.matchMedia = original;
      wide.restore();
    }
  });

  it("shows presets and the no-fields note for an agent without context fields", async () => {
    vi.mocked(getRequestContext).mockResolvedValue({ ...TIER, fields: [], schema: { type: "object", properties: {} } });
    render(<ChatTab agentId="a1" />);
    await waitFor(() => expect(ctxButton()).toHaveAccessibleName(/No context fields/));
    fireEvent.click(ctxButton());
    const dialog = await screen.findByRole("dialog", { name: "Request context" });
    expect(within(dialog).getByTestId("context-no-fields")).toBeInTheDocument();
    expect(within(dialog).getByTestId("context-presets")).toBeInTheDocument();
  });
});

describe("tool-call cards", () => {
  const T0 = "2026-11-14T10:00:00.000Z";
  const T1 = "2026-11-14T10:00:01.400Z";
  const base = [ev(0, "run_started", { input: "go" }, T0)];
  const call = (id: string, name: string, ts = T0) => ev(10, "tool_call", { tool_call_id: id, tool_name: name, args: { title: "Printer" } }, ts);
  const result = (id: string, name: string, status: string, content: unknown, ts = T1) =>
    ev(11, "tool_result", { tool_call_id: id, tool_name: name, status, content }, ts);

  async function show(events: RunEvent[]) {
    mockConversation(["r1"], { r1: [...base, call("k1", "tickets__create"), ...events, ev(20, "model_message", { model_request: 1, text: "done" }, T1)] });
    await renderChat({ conversationId: "c1" });
    return screen.findByTestId("tool-call-card");
  }

  it("starts collapsed, expands to arguments and result in mono, and collapses again", async () => {
    const card = await show([result("k1", "tickets__create", "ok", { ticket_id: "T-42" })]);
    const trigger = within(card).getByRole("button", { name: /tickets__create/ });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(within(card).queryByTestId("tool-call-args")).toBeNull();
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(within(card).getByTestId("tool-call-args")).toHaveTextContent('"title": "Printer"');
    expect(within(card).getByTestId("tool-call-result")).toHaveTextContent('"ticket_id": "T-42"');
    expect(within(card).getByTestId("tool-call-args")).toHaveClass("font-mono");
    expect(within(card).getByTestId("tool-call-result")).toHaveClass("font-mono");
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(within(card).queryByTestId("tool-call-result")).toBeNull();
  });

  it("shows status and duration in the header without expanding", async () => {
    const card = await show([result("k1", "tickets__create", "ok", {})]);
    expect(card).toHaveAttribute("data-status", "ok");
    expect(within(card).getByTestId("tool-call-status")).toHaveTextContent("Done");
    expect(within(card).getByTestId("tool-call-duration")).toHaveTextContent("1.40 s");
  });

  it("marks a failed call as an error", async () => {
    const card = await show([result("k1", "tickets__create", "error", "boom")]);
    expect(card).toHaveAttribute("data-status", "error");
    expect(within(card).getByTestId("tool-call-status")).not.toHaveTextContent("Done");
  });

  it("a call without a result is no_result once the turn ended, with no duration", async () => {
    const card = await show([]);
    expect(card).toHaveAttribute("data-status", "no_result");
    expect(within(card).queryByTestId("tool-call-duration")).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: /tickets__create/ }));
    expect(within(card).getByTestId("tool-call-args")).toBeInTheDocument();
    expect(within(card).queryByTestId("tool-call-result")).toBeNull();
  });
});

describe("/request-context redirect variants", () => {
  const go = async (query: Record<string, string | string[] | undefined>) => {
    const { default: Page } = await import("@/app/request-context/page");
    try {
      await Page({ searchParams: Promise.resolve(query) });
      return null;
    } catch (error) {
      return (error as { redirectTo?: string }).redirectTo ?? null;
    }
  };

  it.each([
    [{ agent: "a1", version: "2" }, "/agents/a1/chat?version=2&context=open"],
    [{ agent: "a1", version: "draft" }, "/agents/a1/chat?version=draft&context=open"],
    [{ agent: "a1" }, "/agents/a1/chat?context=open"],
    [{ agent: "a1", version: "junk" }, "/agents/a1/chat?context=open"],
    [{ agent: "a1", version: "0" }, "/agents/a1/chat?context=open"],
    // A repeated parameter: the first value wins, as for /playground.
    [{ agent: "a1", version: ["3", "4"] }, "/agents/a1/chat?version=3&context=open"],
    [{ agent: ["a1", "a2"], version: "2" }, "/agents/a1/chat?version=2&context=open"],
    [{ agent: "a1", other: "x" }, "/agents/a1/chat?context=open"],
    [{ agent: "a 1/b" }, "/agents/a%201%2Fb/chat?context=open"],
    [{}, "/agents"],
    [{ version: "2" }, "/agents"],
    [{ agent: ".." }, "/agents"],
    [{ agent: "" }, "/agents"],
  ])("%j goes to %s", async (query, target) => {
    expect(await go(query)).toBe(target);
  });

  it("the target is not itself a legacy URL, and the nav no longer lists the page", async () => {
    expect(legacyTarget("/agents/a1/chat", { context: "open" })).toBeNull();
    const { navItems } = await import("@/lib/nav");
    expect(navItems.some((i) => i.href.startsWith("/request-context"))).toBe(false);
  });
});

const traceOf = (runId: string): Trace =>
  ({
    run: {
      run_id: runId, kind: "agent", agent_id: "a1", version: 2, tool_id: null, workflow_id: null, workflow_version: null, status: "completed",
      display_status: "completed", input: "hi", output: "ok", error: null, heartbeat_at: null, created_at: "2026-11-14T10:00:00Z",
      updated_at: "2026-11-14T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
    },
    spans: [{ id: "root", parent_id: null, kind: "run", name: `run ${runId}`, start: "2026-11-14T10:00:00Z", end: "2026-11-14T10:00:05Z", status: "ok", attributes: { run_kind: "agent", run_id: runId }, input: null, output: null, truncated: {} }],
    totals: { runs: 1, model_requests: 0, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 5000, status: "completed" },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  }) as unknown as Trace;

function inPage(body: React.ReactNode) {
  media = mockMatchMedia(true);
  return render(
    <FrameHarness>
      <AgentLayout agentId="a1">{body}</AgentLayout>
    </FrameHarness>,
  );
}

describe("Open trace opens the trace Sheet in place", () => {
  beforeEach(() => {
    vi.mocked(getTrace).mockImplementation(async (id: string) => traceOf(id));
    vi.mocked(getRun).mockImplementation(async (id: string) => traceOf(id).run as never);
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [{ run_id: "r1", agent_id: "a1", status: "completed", display_status: "completed", version: 2, draft: false, created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:05Z", conversation_id: null, turn: null, model_id: "openai:gpt-4.1-mini", model_overridden: false, input_tokens: 1, output_tokens: 1, cost_usd: "0.001" } as unknown as TraceRun], next_before: null });
  });

  it("from a turn's footer: the Sheet opens over the chat, the URL gains ?trace=, the page and composer stay", async () => {
    mockConversation(["r1"]);
    window.history.replaceState({}, "", routes.agentChat("a1", { conversation: "c1" }));
    inPage(<ChatTab agentId="a1" conversationId="c1" />);
    const link = await screen.findByRole("link", { name: "Open trace" });
    expect(link).toHaveAttribute("href", routes.trace("r1"));
    const composer = screen.getByTestId("chat-composer");
    fireEvent.click(link);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).getByTestId("trace-sheet-run-id")).toHaveTextContent("r1");
    expect(window.location.pathname).toMatch(/^\/agents\/a1\/chat/);
    expect(search().get("trace")).toBe("r1");
    expect(search().get("conversation")).toBeNull();
    expect(screen.getAllByTestId("trace-sheet")).toHaveLength(1);
    expect(screen.getByTestId("chat-composer")).toBe(composer);
    expect(screen.getByTestId("chat-pane")).toBeInTheDocument();
  });

  it("a modified click is left to the browser (new tab), opening no Sheet", async () => {
    mockConversation(["r1"]);
    inPage(<ChatTab agentId="a1" conversationId="c1" />);
    const link = await screen.findByRole("link", { name: "Open trace" });
    let handled: boolean | null = null;
    const stop = (e: Event) => {
      handled = e.defaultPrevented;
      e.preventDefault(); // jsdom cannot navigate
    };
    document.addEventListener("click", stop);
    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true }));
    document.removeEventListener("click", stop);
    expect(handled).toBe(false);
    expect(screen.queryByTestId("trace-sheet")).toBeNull();
    expect(search().has("trace")).toBe(false);
  });

  it("on the Traces tab there is exactly one trace Sheet, whichever control opens it", async () => {
    nav.path = routes.agentTraces("a1");
    window.history.replaceState({}, "", routes.agentTraces("a1"));
    inPage(<TracesTab agentId="a1" />);
    const list = await screen.findByTestId("agent-traces-list");
    fireEvent.click(within(list).getAllByRole("button", { name: /^Run started/ })[0]);
    await screen.findByTestId("trace-sheet");
    expect(screen.getAllByTestId("trace-sheet")).toHaveLength(1);
    expect(search().get("trace")).toBe("r1");
    fireEvent.keyDown(screen.getByTestId("trace-sheet"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("trace-sheet")).toBeNull());
    expect(search().has("trace")).toBe(false);
  });
});

describe("no hard-coded colours in the chat", () => {
  const src = path.resolve(__dirname, "..");
  const dirs = [path.join(src, "chat"), path.join(src, "requestContext"), __dirname];
  const files = dirs.flatMap((d) => fs.readdirSync(d).filter((f) => /\.tsx?$/.test(f) && !/\.test\./.test(f)).map((f) => path.join(d, f)));
  const palette = "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose";
  const forbidden = [
    new RegExp(`\\b[a-z:-]*(?:bg|text|border|ring|fill|stroke|from|to|via|divide|outline|shadow|decoration|accent|caret)-(?:${palette})-\\d`),
    /\b(?:bg|text|border|ring|fill|stroke)-(?:white|black)\b/,
    /#[0-9a-fA-F]{3,8}\b/,
    /\b(?:rgb|rgba|hsl|hsla|oklch)\(/,
  ];

  it("scans the chat components, the popover and the agent page's chat files", () => {
    const names = files.map((f) => path.basename(f));
    for (const needed of ["messageParts.tsx", "TurnFooter.tsx", "ConversationRail.tsx", "RequestContextPopover.tsx", "ChatPane.tsx", "Composer.tsx", "ComposerChip.tsx", "ChatTab.tsx"]) {
      expect(names).toContain(needed);
    }
  });

  it("uses tokens only, and the old playground folder is gone", () => {
    const bad: string[] = [];
    for (const file of files) {
      fs.readFileSync(file, "utf8").split("\n").forEach((line, i) => {
        if (/^\s*(\/\/|\*|\/\*)/.test(line)) return;
        if (forbidden.some((re) => re.test(line))) bad.push(`${path.relative(src, file)}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(bad).toEqual([]);
    expect(fs.existsSync(path.join(src, "playground"))).toBe(false);
    expect(fs.readFileSync(path.join(__dirname, "ChatPane.tsx"), "utf8")).toContain("bg-fill-selected");
  });
});

describe.each(["light", "dark"] as const)("axe on the chat page (%s theme)", (theme) => {
  beforeEach(() => {
    window.localStorage.setItem("studio.theme", theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
  });

  it("has no violations and exactly one h1, on the landing and with the popover open", async () => {
    inPage(<ChatTab agentId="a1" />);
    await waitFor(() => expect(ctxButton()).toHaveAccessibleName(/of 1 set/));
    expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark");
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    await expectNoAxeViolations(document.body);
    fireEvent.click(ctxButton());
    await screen.findByRole("dialog", { name: "Request context" });
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    await act(async () => {
      await expectNoAxeViolations(document.body);
    });
  }, AXE_TIMEOUT_MS);

  it("has no violations and one h1 with a transcript, a tool call card and a turn footer", async () => {
    mockConversation(["r1"], { r1: [ev(0, "run_started", { input: "go" }), ev(1, "tool_call", { tool_call_id: "k", tool_name: "tickets__create", args: { a: 1 } }), ev(2, "tool_result", { tool_call_id: "k", tool_name: "tickets__create", status: "ok", content: { id: 1 } }, "2026-11-14T10:00:01Z"), ev(3, "model_message", { model_request: 1, text: "done" })] });
    inPage(<ChatTab agentId="a1" conversationId="c1" />);
    const card = await screen.findByTestId("tool-call-card");
    fireEvent.click(within(card).getByRole("button", { name: /tickets__create/ }));
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
