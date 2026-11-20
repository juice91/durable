// RM.17 acceptance: the editor's Knowledge and Memory steps through the real EditorTab: publish with
// knowledge bases and every memory feature (the exact request body, pinned to the API's own dump),
// reload, on-then-off, server 422 routing with the API's real messages, models outside the
// allowlist, lazy lists, Config panel links, phone width, axe and the unsaved-changes guard. The
// implementer's knowledgeMemory.test.ts, knowledgeMemorySteps.test.tsx and configKnowledgeMemory.test.tsx
// cover the helpers, the steps in isolation and the Config sections.
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Final review MINOR 3: these full-stepper renders timed out at the 5 s default when the machine was
// loaded (other suites running); 76/76 pass alone. A longer budget, not a weaker assertion.
vi.setConfig({ testTimeout: 30_000 });

const nav = vi.hoisted(() => ({ search: "", path: "", push: vi.fn(), replace: vi.fn() }));
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(), listEmbeddingModels: vi.fn() }));
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
// The editor also mounts the attached-scorers section; this suite isn't about it, so its lists are empty.
vi.mock("@/lib/scorers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/scorers")>();
  return { ...actual, listAgentScorers: vi.fn(() => Promise.resolve([])), listReusableScorers: vi.fn(() => Promise.resolve([])) };
});
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(() => Promise.resolve([])), test: vi.fn() } };
});
vi.mock("@/components/agents/specSections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/agents/specSections")>();
  return { ...actual, fetchProcessorCatalog: vi.fn(), fetchModels: vi.fn() };
});

import { api, ApiError, type AgentSpec } from "@/lib/api";
import { listEmbeddingModels, listKnowledgeBases, type KnowledgeBase } from "@/lib/knowledge";
import { routes } from "@/lib/routes";
import { fetchModels, fetchProcessorCatalog } from "@/components/agents/specSections";
import { modelsFixture, processorCatalogFixture } from "@/components/agents/specSections.fixtures";
import { setWindowWidth, TEST_WIDE_WIDTH } from "@/components/frame/frameTesting";
import { SidePanel, SidePanelProvider } from "@/components/frame/SidePanel";
import { ConfigPanel } from "@/components/agentPage/ConfigPanel";
import { EditorTab } from "@/components/agentPage/EditorTab";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { EDITOR_STEPS, stepForError } from "./registry";
import { findSelectOption, getSelectOption, optionsOf } from "@/test/select";

const KB1 = "11111111-1111-4111-8111-111111111111";
const KB2 = "22222222-2222-4222-8222-222222222222";
const kb = (id: string, name: string, extra: Partial<KnowledgeBase> = {}) =>
  ({ id, name, description: null, embedding_model: "test:hash", dimensions: 1536, document_count: 3, ...extra }) as KnowledgeBase;
const BASES = [kb(KB1, "Product docs", { description: "Manuals" }), kb(KB2, "FAQ", { document_count: 1 })];
const EMBEDDINGS = [
  { id: "test:hash", provider: "test", name: "hash", dimensions: 1536, wildcard: false },
  { id: "litellm:*", provider: "litellm", name: "*", dimensions: 1024, wildcard: true },
];
const PLAIN = {
  spec_version: 1,
  name: "Support bot",
  instructions: "Help people",
  model: { provider: "test", name: "echo" },
  tools: [],
  model_settings: { temperature: null, max_tokens: null },
} as unknown as AgentSpec;
const SCHEMA = { type: "object", properties: { name: { type: "string" }, goals: { type: "array", items: { type: "string" } } } };
// The API's own dump (`AgentSpec.model_validate(...).model_dump(mode="json")`, run with eap_schemas) of the
// spec this file builds in the editor: the editor's request body must be this string, byte for byte.
const SERVER_DUMP_FULL =
  '{"spec_version":1,"name":"Support bot","instructions":"Help people","model":{"provider":"test","name":"echo"},"tools":[],"model_settings":{"temperature":null,"max_tokens":null},"knowledge_bases":[{"id":"11111111-1111-4111-8111-111111111111","top_k":5,"min_score":0.3,"filter":{"lang":"en","tag":["faq","guide"]}},{"id":"22222222-2222-4222-8222-222222222222","top_k":8}],"memory":{"history":{"last_turns":12,"max_tokens":4000},"working":{"scope":"conversation","json_schema":{"type":"object","properties":{"name":{"type":"string"},"goals":{"type":"array","items":{"type":"string"}}}},"read_only":false},"observational":{"model":{"provider":"test","name":"echo"},"observe_after_tokens":12000,"keep_recent_turns":4,"reflect_after_tokens":8000},"semantic_recall":{"embedding_model":"test:hash","top_k":4,"message_range":1,"scope":"resource","min_score":0.5},"redact":{"kinds":["email","phone"],"action":"redact"}}}';
const FULL = JSON.parse(SERVER_DUMP_FULL) as AgentSpec;

const version = (n: number, spec: AgentSpec) => ({ agent_id: "a1", version: n, spec, spec_hash: `hash${n}000000`, created_at: "2026-11-11T10:00:00Z", created_by: "alice" });
const agentWith = (spec: AgentSpec) => ({ id: "a1", name: spec.name, draft_spec: spec, latest_version: 1 });
/** The stored draft the next load returns; a save stores what it was sent (as the API does after its own dump). */
let stored: AgentSpec = PLAIN;
const seedStored = (spec: AgentSpec) => {
  stored = spec;
  vi.mocked(api.agents.get).mockImplementation(async () => agentWith(stored) as never);
  vi.mocked(api.versions.list).mockImplementation(async () => [version(1, stored)] as never);
};

let restoreWidth: () => void = () => undefined;
beforeEach(() => {
  vi.clearAllMocks();
  nav.search = "";
  nav.path = routes.agentEditor("a1");
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  vi.mocked(listKnowledgeBases).mockResolvedValue(BASES);
  vi.mocked(listEmbeddingModels).mockResolvedValue(EMBEDDINGS);
  vi.mocked(fetchModels).mockResolvedValue(modelsFixture);
  vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
  vi.mocked(api.agents.update).mockImplementation(async (_id, spec) => {
    stored = spec;
    return agentWith(spec) as never;
  });
  vi.mocked(api.versions.create).mockResolvedValue(version(2, stored) as never);
  seedStored(PLAIN);
});
afterEach(() => restoreWidth());

const stepNav = () => screen.getByRole("navigation", { name: "Editor steps" });
const navButton = (label: RegExp) => within(stepNav()).getByRole("button", { name: label });
const region = (label: string) => screen.getByRole("region", { name: label });
const byId = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const set = (id: string, value: string) => fireEvent.change(byId(id), { target: { value } });
const click = (id: string) => fireEvent.click(byId(id));
/** The shared ModelPicker: choose the provider in its select, then the model in its searchable combobox. */
async function pickModel(modelId: string, provider: string, name: string, { hidden = false } = {}) {
  const providerId = modelId.replace(/-model$/, "-provider");
  await findSelectOption(new RegExp(`^${provider}\\b`));
  set(providerId, provider);
  await waitFor(() => expect(byId(modelId)).toBeEnabled());
  fireEvent.click(byId(modelId));
  fireEvent.click(await screen.findByRole("option", { name, hidden }));
  await waitFor(() => expect(byId(modelId)).toHaveTextContent(name));
}
const saveButton = () => screen.getByRole("button", { name: "Save draft" });
const publishButton = () => screen.getByRole("button", { name: "Publish" });
/** The spec the last save sent. */
const sent = () => vi.mocked(api.agents.update).mock.calls.at(-1)![1] as unknown as Record<string, unknown>;
const unload = () => {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
};

async function renderEditor(step = "", ui: React.ReactElement = <EditorTab agentId="a1" />) {
  nav.search = step ? `step=${step}` : "";
  const view = render(ui);
  await screen.findByTestId("agent-stepper");
  return view;
}
const goTo = (label: RegExp) => fireEvent.click(navButton(label));
async function save() {
  fireEvent.click(saveButton());
  await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
  await waitFor(() => expect(screen.queryByText("Save the draft before publishing")).toBeNull());
}

/** Builds `FULL` by hand in the editor: two knowledge bases, then every memory feature. */
async function buildFull() {
  goTo(/^Knowledge/);
  fireEvent.click(await screen.findByRole("checkbox", { name: "Product docs" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "FAQ" }));
  set("kb-0-top_k", "5");
  set("kb-0-min_score", "0.3");
  set("kb-0-filter", '{"lang": "en", "tag": ["faq", "guide"]}');
  goTo(/^Memory/);
  click("memory-enabled");
  set("memory-history-last_turns", "12");
  set("memory-history-max_tokens", "4000");
  click("memory-working-enabled");
  fireEvent.click(screen.getByRole("radio", { name: "Per conversation" }));
  fireEvent.click(screen.getByRole("radio", { name: /^JSON schema/ }));
  set("memory-working-schema", JSON.stringify(SCHEMA));
  click("memory-observational-enabled");
  await pickModel("memory-observational-model", "test", "echo");
  click("memory-recall-enabled");
  await waitFor(() => expect(optionsOf(byId("memory-recall-embedding_model")).length).toBeGreaterThan(1));
  set("memory-recall-embedding_model", "test:hash");
  set("memory-recall-min_score", "0.5");
  fireEvent.click(screen.getByRole("radio", { name: "Choose kinds" }));
  for (const kind of ["credit_card", "iban", "ipv4", "us_ssn"]) fireEvent.click(screen.getByRole("checkbox", { name: kind }));
}

describe("publish with knowledge and every memory feature", () => {
  it("sends exactly the API's own dump of the spec, then publishes it", async () => {
    await renderEditor();
    await buildFull();
    expect(publishButton()).toBeDisabled();
    expect(screen.getByText("Save the draft before publishing")).toBeInTheDocument();
    await save();
    expect(api.agents.update).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.agents.update).mock.calls[0][0]).toBe("a1");
    expect(JSON.stringify(sent())).toBe(SERVER_DUMP_FULL);
    expect(sent()).toEqual(FULL);
    expect(publishButton()).toBeEnabled();
    fireEvent.click(publishButton());
    await waitFor(() => expect(api.versions.create).toHaveBeenCalledWith("a1"));
    expect(await screen.findByText(/Version 2 published/)).toBeInTheDocument();
  });
});

describe("reload and clean drafts", () => {
  it("a reloaded draft saves the same bytes, from any step, and stays clean", async () => {
    seedStored(FULL);
    const first = await renderEditor();
    for (const step of EDITOR_STEPS) goTo(new RegExp(`^${step.label}`));
    expect(screen.queryByText("Save the draft before publishing")).toBeNull();
    expect(unload()).toBe(false);
    fireEvent.click(saveButton());
    await waitFor(() => expect(api.agents.update).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(sent())).toBe(SERVER_DUMP_FULL);
    first.unmount();
    vi.mocked(api.agents.update).mockClear();
    await renderEditor("memory");
    fireEvent.click(saveButton());
    await waitFor(() => expect(api.agents.update).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(sent())).toBe(SERVER_DUMP_FULL);
  });

  it("build, save, reload: the second editor shows what the first saved, and its save is byte-identical", async () => {
    const first = await renderEditor();
    await buildFull();
    await save();
    first.unmount();
    vi.mocked(api.agents.update).mockClear();
    await renderEditor("knowledge");
    expect(await screen.findByRole("heading", { name: "Product docs" })).toBeInTheDocument();
    expect(byId<HTMLInputElement>("kb-0-top_k")).toHaveValue(5);
    expect(navButton(/^Knowledge/)).toHaveTextContent("2 knowledge bases");
    expect(navButton(/^Memory/)).toHaveTextContent("History 12 · Working · Observational · Recall");
    fireEvent.click(saveButton());
    await waitFor(() => expect(api.agents.update).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(sent())).toBe(SERVER_DUMP_FULL);
  });

  it("memory on with everything, then off: the draft is the original again (no key, not {}) and clean", async () => {
    await renderEditor();
    await buildFull();
    expect(screen.getByText("Save the draft before publishing")).toBeInTheDocument();
    goTo(/^Knowledge/);
    fireEvent.click(screen.getByRole("button", { name: "Detach Product docs" }));
    fireEvent.click(screen.getByRole("button", { name: "Detach FAQ" }));
    goTo(/^Memory/);
    click("memory-enabled");
    expect(screen.queryByText("Save the draft before publishing")).toBeNull();
    expect(unload()).toBe(false);
    expect(publishButton()).toBeEnabled();
    expect(navButton(/^Memory/)).toHaveTextContent("Off");
    expect(navButton(/^Knowledge/)).toHaveTextContent("None");
    fireEvent.click(saveButton());
    await waitFor(() => expect(api.agents.update).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(sent())).toBe(JSON.stringify(PLAIN));
    expect(sent()).not.toHaveProperty("memory");
    expect(sent()).not.toHaveProperty("knowledge_bases");
  });

  it("memory on with nothing else is {}; turning it off again removes the key (never {} or null)", async () => {
    await renderEditor("memory");
    click("memory-enabled");
    await save();
    expect(JSON.stringify(sent())).toBe(JSON.stringify({ ...PLAIN, memory: {} }));
    expect(navButton(/^Memory/)).toHaveTextContent("History 20");
    click("memory-enabled");
    expect(screen.getByText("Save the draft before publishing")).toBeInTheDocument();
    await save();
    expect(JSON.stringify(sent())).toBe(JSON.stringify(PLAIN));
    expect("memory" in sent()).toBe(false);
  });

  it("a feature turned on and off again inside memory leaves only that feature's key out", async () => {
    seedStored({ ...PLAIN, memory: { history: { last_turns: 12 } } } as unknown as AgentSpec);
    await renderEditor("memory");
    for (const id of ["memory-working-enabled", "memory-observational-enabled", "memory-recall-enabled"]) {
      click(id);
      click(id);
    }
    expect(screen.queryByText("Save the draft before publishing")).toBeNull();
    fireEvent.click(saveButton());
    await waitFor(() => expect(api.agents.update).toHaveBeenCalledTimes(1));
    expect(sent().memory).toEqual({ history: { last_turns: 12 } });
  });
});

// The 422 detail as lib/api.ts shows it: a route's string as is, a validator's messages joined with ", ".
// Every message but the last two probes was produced by eap_schemas, eap_api's publish checks or the
// model/embedding policies (run with the real code), not written by hand.
const ALLOW = "(not in the EAP_ALLOWED_MODELS allowlist)";
const EMB_ALLOW = "(not in the EAP_ALLOWED_EMBEDDING_MODELS allowlist)";
const ROUTED: [string, string][] = [
  [`knowledge base '${KB2}' does not exist in this tenant`, "Knowledge"],
  [`Value error, duplicate knowledge base: ${KB1}`, "Knowledge"],
  ["Value error, knowledge base id must be a UUID", "Knowledge"],
  ["Value error, filter must be an object", "Knowledge"],
  ["Value error, filter has more than 16 keys", "Knowledge"],
  ["Value error, filter.tag: a list needs 1 to 32 values", "Knowledge"],
  ["Value error, filter.k: string longer than 256 characters", "Knowledge"],
  ["Value error, filter.k: value must be a string, number or boolean", "Knowledge"],
  ["Value error, metadata key must match ^[A-Za-z0-9_.-]{1,64}$", "Knowledge"],
  ["Value error, template cannot be blank", "Memory"],
  ["Value error, template must be at most 4096 bytes in UTF-8", "Memory"],
  ["Value error, set either template or json_schema, not both", "Memory"],
  ["Value error, working memory is invalid at a: unsupported type 'date'", "Memory"],
  ["Value error, working memory is invalid at a.a.a: objects may be nested at most 3 deep", "Memory"],
  ["Value error, working memory is invalid: the schema must be at most 8192 bytes", "Memory"],
  ["Value error, invalid embedding model id 'nocolon': expected 'provider:name'", "Memory"],
  ["Value error, memory.redact must use action 'redact' (memory writes are never blocked)", "Memory"],
  [`observer model 'test:nope' is not allowed on this platform ${ALLOW}`, "Memory"],
  [`memory.semantic_recall: embedding model 'litellm:x' is not allowed on this platform ${EMB_ALLOW}`, "Memory"],
  ["memory.semantic_recall: embedding model 'x:y' is allowed by a wildcard but its dimensions are unknown; add it to EAP_ALLOWED_EMBEDDING_MODELS as 'x:y=<768|1024|1536>'", "Memory"],
  ["Value error, tool name 'retrieve' is reserved for a platform tool", "Tools"],
  ["tool name 'update_working_memory' is reserved for a platform tool", "Tools"],
  // Words the new steps look for, inside messages other steps own.
  ["instructions: placeholder {{context.memory}} names no context field", "Instructions"],
  ["input_processors[0].config.patterns: knowledge base filter template is not valid", "Processors"],
  ["Value error, duplicate context field: filter", "Request context"],
];
// Real messages with no field name in them (pydantic's own bounds); the editor checks these limits
// before saving, so a server never sends them for a Studio draft, and no step claims them.
const UNOWNED = ["Input should be less than or equal to 20", "Tuple should have at most 8 items after validation, not 9", "Input should be 'resource' or 'conversation'"];

describe("server 422s open the step that owns them", () => {
  const edit = () => fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Support bot 2" } });

  it.each(ROUTED)("%s -> %s", async (message, label) => {
    vi.mocked(api.agents.update).mockRejectedValueOnce(new ApiError(422, message));
    await renderEditor();
    edit();
    fireEvent.click(saveButton());
    await waitFor(() => expect(region(label)).toBeVisible());
    expect(navButton(new RegExp(`^${label}`))).toHaveTextContent("Has an error");
    expect(navButton(new RegExp(`^${label}`))).toHaveAccessibleName(/has an error/);
    expect(within(stepNav()).getAllByText("Has an error")).toHaveLength(1);
    expect(stepForError(message)).toBe(EDITOR_STEPS.find((s) => s.label === label)!.id);
  });

  it.each(UNOWNED)("an unowned message %j stays on the step and shows the alert", async (message) => {
    vi.mocked(api.agents.update).mockRejectedValueOnce(new ApiError(422, message));
    await renderEditor("knowledge");
    edit();
    fireEvent.click(saveButton());
    expect(await screen.findByTestId("agent-save-error")).toHaveTextContent(message);
    expect(region("Knowledge")).toBeVisible();
    expect(within(stepNav()).queryByText("Has an error")).toBeNull();
    expect(stepForError(message)).toBeNull();
  });

  it("a knowledge error then a memory error moves between the steps, one marked at a time", async () => {
    vi.mocked(api.agents.update).mockRejectedValueOnce(new ApiError(422, ROUTED[0][0])).mockRejectedValueOnce(new ApiError(422, ROUTED[9][0]));
    await renderEditor();
    edit();
    fireEvent.click(saveButton());
    await waitFor(() => expect(region("Knowledge")).toBeVisible());
    // The first save must have finished (Save enabled again) before the second click counts.
    await waitFor(() => {
      expect(api.agents.update).toHaveBeenCalledTimes(1);
      expect(saveButton()).toBeEnabled();
    });
    fireEvent.click(saveButton());
    await waitFor(() => expect(region("Memory")).toBeVisible());
    expect(within(stepNav()).getAllByText("Has an error")).toHaveLength(1);
    expect(navButton(/^Knowledge/)).not.toHaveTextContent("Has an error");
  });
});

describe("a stored model outside the allowlist", () => {
  const OUTSIDE = {
    ...PLAIN,
    memory: {
      observational: { model: { provider: "test", name: "gone" }, observe_after_tokens: 12000, keep_recent_turns: 4, reflect_after_tokens: 8000 },
      semantic_recall: { embedding_model: "other:gone", top_k: 4, message_range: 1, scope: "resource" },
    },
  } as unknown as AgentSpec;
  const select = (id: string) => byId<HTMLSelectElement>(id);
  const marked = async (id: string) => {
    await waitFor(() => expect(byId(id)).toHaveAttribute("aria-invalid", "true"));
    expect(byId(id)).toHaveAccessibleDescription(/Not in the platform allowlist/);
  };

  it("stays selected and marked on both pickers, without blocking the step", async () => {
    seedStored(OUTSIDE);
    await renderEditor("memory");
    await marked("memory-observational-model");
    await marked("memory-recall-embedding_model");
    expect(byId("memory-observational-provider")).toHaveValue("test");
    expect(byId("memory-observational-model")).toHaveTextContent("gone (not listed)");
    expect(select("memory-recall-embedding_model")).toHaveValue("other:gone");
    expect(getSelectOption("other:gone (not in the allowlist)")).toBeTruthy();
    expect(navButton(/^Memory/)).toHaveTextContent("History 20 · Observational · Recall");
  });

  it("is saved untouched, and survives edits to the other fields of its feature", async () => {
    seedStored(OUTSIDE);
    await renderEditor("memory");
    await marked("memory-observational-model");
    set("memory-observational-keep_recent_turns", "6");
    set("memory-recall-top_k", "3");
    await save();
    const memory = sent().memory as Record<string, Record<string, unknown>>;
    expect(memory.observational.model).toEqual({ provider: "test", name: "gone" });
    expect(memory.observational.keep_recent_turns).toBe(6);
    expect(memory.semantic_recall.embedding_model).toBe("other:gone");
    expect(memory.semantic_recall.top_k).toBe(3);
    expect(byId("memory-observational-model")).toHaveTextContent("gone (not listed)");
  });

  it("picking an allowlisted model replaces it, clears the mark and is saved", async () => {
    seedStored(OUTSIDE);
    await renderEditor("memory");
    await marked("memory-observational-model");
    await pickModel("memory-observational-model", "test", "echo");
    set("memory-recall-embedding_model", "test:hash");
    expect(byId("memory-observational-model")).not.toHaveAttribute("aria-invalid");
    expect(select("memory-recall-embedding_model")).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByRole("option", { name: /not in the allowlist/ })).toBeNull();
    await save();
    expect((sent().memory as Record<string, Record<string, unknown>>).observational.model).toEqual({ provider: "test", name: "echo" });
  });

  it("an unchanged outside model is still rejected by the server on publish, and the error opens Memory", async () => {
    seedStored(OUTSIDE);
    vi.mocked(api.versions.create).mockRejectedValueOnce(new ApiError(422, `observer model 'test:gone' is not allowed on this platform ${ALLOW}`));
    await renderEditor("identity");
    fireEvent.click(publishButton());
    await waitFor(() => expect(region("Memory")).toBeVisible());
    expect(navButton(/^Memory/)).toHaveTextContent("Has an error");
    expect(within(region("Memory")).getByTestId("agent-save-error")).toHaveTextContent("Observer model 'test:gone' is not allowed");
  });
});

describe("lists load lazily", () => {
  const idle = () => act(async () => undefined);

  it("none is requested until its step is shown, and each once", async () => {
    await renderEditor();
    await idle();
    expect(listKnowledgeBases).not.toHaveBeenCalled();
    expect(listEmbeddingModels).not.toHaveBeenCalled();
    goTo(/^Knowledge/);
    await screen.findByText("Product docs");
    expect(listKnowledgeBases).toHaveBeenCalledTimes(1);
    expect(listEmbeddingModels).not.toHaveBeenCalled();
    goTo(/^Memory/);
    await waitFor(() => expect(listEmbeddingModels).toHaveBeenCalledTimes(1));
    goTo(/^Knowledge/);
    goTo(/^Memory/);
    goTo(/^Identity/);
    await idle();
    expect(listKnowledgeBases).toHaveBeenCalledTimes(1);
    expect(listEmbeddingModels).toHaveBeenCalledTimes(1);
  });

  it("a spec that uses them loads them at once, so names show on every step", async () => {
    seedStored(FULL);
    await renderEditor("identity");
    await waitFor(() => expect(listKnowledgeBases).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listEmbeddingModels).toHaveBeenCalledTimes(1));
    goTo(/^Knowledge/);
    expect(await screen.findByRole("heading", { name: "Product docs" })).toBeInTheDocument();
    expect(listKnowledgeBases).toHaveBeenCalledTimes(1);
  });

  it("the Config panel asks for names only when the spec attaches a knowledge base", async () => {
    const empty = render(<ConfigPanel agent={agentWith(PLAIN) as never} versions={[version(1, PLAIN)] as never} />);
    await idle();
    expect(listKnowledgeBases).not.toHaveBeenCalled();
    expect(screen.getByRole("region", { name: /^Knowledge/ })).toHaveTextContent("None");
    expect(screen.getByRole("region", { name: /^Memory/ })).toHaveTextContent("Off");
    empty.unmount();
    render(<ConfigPanel agent={agentWith(FULL) as never} versions={[version(1, FULL)] as never} />);
    expect(await screen.findByText("Product docs")).toBeInTheDocument();
    expect(screen.getByText("FAQ")).toBeInTheDocument();
    expect(listKnowledgeBases).toHaveBeenCalledTimes(1);
  });

  it("a failed list keeps the attachments by id and saves the same bytes", async () => {
    seedStored(FULL);
    vi.mocked(listKnowledgeBases).mockRejectedValue(new Error("boom"));
    await renderEditor("knowledge");
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load knowledge bases: boom");
    expect(screen.getByRole("heading", { name: KB1 })).toBeInTheDocument();
    expect(screen.queryByText(/does not exist in this tenant/)).toBeNull();
    fireEvent.click(saveButton());
    await waitFor(() => expect(api.agents.update).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(sent())).toBe(SERVER_DUMP_FULL);
  });
});

describe("Config panel Edit links open steps 6 and 7", () => {
  const stepOf = (href: string) => new URL(href, "http://localhost").searchParams.get("step");
  it.each([
    ["Edit knowledge", "knowledge", "Knowledge", "6"],
    ["Edit memory", "memory", "Memory", "7"],
  ])("%s links to ?step=%s and the editor opens %s as step %s", async (name, id, label, position) => {
    const config = render(<ConfigPanel agent={agentWith(FULL) as never} versions={[version(1, FULL)] as never} />);
    const href = screen.getByRole("link", { name }).getAttribute("href")!;
    expect(href).toBe(routes.agentEditor("a1", { step: id }));
    expect(stepOf(href)).toBe(id);
    config.unmount();
    expect(EDITOR_STEPS.findIndex((s) => s.id === id) + 1).toBe(Number(position));
    nav.search = new URL(href, "http://localhost").search.slice(1);
    await renderEditor(new URL(href, "http://localhost").searchParams.get("step")!);
    expect(navButton(new RegExp(`^${label}`))).toHaveAttribute("aria-current", "step");
    expect(region(label)).toBeVisible();
    expect(within(stepNav()).getAllByRole("button")[Number(position) - 1]).toHaveAttribute("aria-current", "step");
  });

  it("the sections show what the editor will show: counts and summaries from the same spec", async () => {
    render(<ConfigPanel agent={agentWith(FULL) as never} versions={[version(1, FULL)] as never} />);
    const knowledge = await screen.findByRole("region", { name: /^Knowledge/ });
    expect(knowledge).toHaveTextContent("Knowledge 2");
    expect(knowledge).toHaveTextContent("Top 5 · min score 0.3 · filter lang=en, tag=faq|guide");
    const memory = screen.getByTestId("config-memory");
    expect(memory).toHaveTextContent("Last 12 turns, at most 4,000 tokens");
    expect(memory).toHaveTextContent("test:echo, observe after 12,000 tokens");
    expect(memory).toHaveTextContent("test:hash, top 4, same user");
    expect(memory).toHaveTextContent("email, phone");
  });
});

describe("unsaved-changes guard with the new steps", () => {
  const link = (href: string) => {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = "elsewhere";
    a.setAttribute("data-guard", "");
    a.addEventListener("click", (event) => event.preventDefault());
    document.body.appendChild(a);
    return a;
  };
  afterEach(() => document.querySelectorAll("a[data-guard]").forEach((a) => a.remove()));
  const attach = async () => {
    goTo(/^Knowledge/);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Product docs" }));
  };

  it("attaching a knowledge base arms it: leaving asks, Stay keeps the edit, Leave goes", async () => {
    await renderEditor();
    expect(unload()).toBe(false);
    await attach();
    expect(unload()).toBe(true);
    const a = link(routes.agentTraces("a1"));
    fireEvent.click(a);
    const dialog = await screen.findByRole("dialog", { name: "Leave without saving?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Stay" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: "Product docs" })).toBeChecked();
    fireEvent.click(a);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Leave without saving" }));
    expect(nav.push).toHaveBeenCalledWith(routes.agentTraces("a1"));
  });

  it("turning memory on arms it, saving disarms it, and turning it off again arms it once more", async () => {
    await renderEditor("memory");
    click("memory-enabled");
    expect(unload()).toBe(true);
    await save();
    expect(unload()).toBe(false);
    fireEvent.click(link(routes.agents()));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    click("memory-enabled");
    expect(unload()).toBe(true);
  });

  it("undoing an edit by hand disarms it: detach after attach, off after on, a typed value put back", async () => {
    await renderEditor();
    await attach();
    expect(unload()).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Detach Product docs" }));
    expect(unload()).toBe(false);
    goTo(/^Memory/);
    await act(async () => undefined);
    click("memory-enabled");
    set("memory-history-last_turns", "7");
    expect(unload()).toBe(true);
    set("memory-history-last_turns", "");
    click("memory-enabled");
    expect(unload()).toBe(false);
  });

  it("stepping between steps never prompts, and an invalid typed value still counts as an edit", async () => {
    seedStored({ ...PLAIN, knowledge_bases: [{ id: KB1, top_k: 8 }] } as unknown as AgentSpec);
    await renderEditor("knowledge");
    await screen.findByRole("heading", { name: "Product docs" });
    set("kb-0-filter", '{"lang": ');
    expect(unload()).toBe(true);
    fireEvent.click(screen.getByTestId("stepper-next"));
    goTo(/^Knowledge/);
    fireEvent.click(link(routes.agentEditor("a1", { step: "memory" })));
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
    fireEvent.click(saveButton());
    await act(async () => undefined);
    expect(api.agents.update).not.toHaveBeenCalled();
    expect(unload()).toBe(true);
    fireEvent.click(link(routes.agents()));
    expect(await screen.findByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
  });

  it("a rejected save keeps the edit armed; the next good save disarms it", async () => {
    vi.mocked(api.agents.update).mockRejectedValueOnce(new ApiError(422, ROUTED[0][0]));
    await renderEditor();
    await attach();
    fireEvent.click(saveButton());
    await waitFor(() => expect(navButton(/^Knowledge/)).toHaveTextContent("Has an error"));
    expect(unload()).toBe(true);
    fireEvent.click(saveButton());
    await waitFor(() => expect(unload()).toBe(false));
    expect(navButton(/^Knowledge/)).not.toHaveTextContent("Has an error");
  });
});

describe("phone width (by classes)", () => {
  const PHONE = 375;
  const sections = () => ["knowledge", "memory"].map((step) => document.querySelector(`section[data-step="${step}"]`) as HTMLElement);
  async function atPhone(step: string) {
    seedStored(FULL);
    restoreWidth();
    restoreWidth = setWindowWidth(PHONE);
    await renderEditor(step);
    await screen.findByRole("heading", { name: "Product docs", hidden: true });
    await waitFor(() => expect(optionsOf(byId("memory-observational-provider")).length).toBeGreaterThan(1));
  }

  it("the step select lists steps 6 and 7 and there is no nav column", async () => {
    await atPhone("memory");
    expect(screen.getByTestId("agent-stepper")).toHaveAttribute("data-layout", "compact");
    expect(screen.queryByRole("navigation", { name: "Editor steps" })).toBeNull();
    const options = optionsOf(screen.getByTestId("stepper-select")).map((o) => o.textContent);
    // AC.13 added step 8 (Output) after Memory.
    expect(options.slice(-3, -1)).toEqual(["6. Knowledge — 2 knowledge bases", "7. Memory — History 12 · Working · Observational · Recall"]);
    fireEvent.change(screen.getByTestId("stepper-select"), { target: { value: "knowledge" } });
    expect(screen.getByText("Step 6 of 8")).toBeInTheDocument();
    expect(region("Knowledge")).toBeVisible();
  });

  it("nothing in either step has a fixed width, a multi-column grid at base size, or can overflow", async () => {
    await atPhone("knowledge");
    const offenders: string[] = [];
    for (const section of sections()) {
      for (const element of [section, ...Array.from(section.querySelectorAll("*"))]) {
        const classes = (element.getAttribute("class") ?? "").split(/\s+/).filter(Boolean);
        for (const name of classes) {
          if (/^(?:min-w|w|max-w)-\[\d+(?:px|rem)\]$/.test(name) || /^(?:min-w|w)-(?:[1-9]\d*|px)$/.test(name)) offenders.push(`${element.tagName}.${name}`);
          if (/^grid-cols-[2-9]\d*$/.test(name)) offenders.push(`${element.tagName}.${name} at base size`);
          // The shared Button is nowrap by design (short labels, in wrapping rows).
          if ((name === "whitespace-nowrap" && element.tagName !== "BUTTON") || name === "overflow-x-scroll") offenders.push(`${element.tagName}.${name}`);
        }
        // Radix keeps an aria-hidden native <select> beside a themed Select inside a form; it is never shown.
        if (["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName) && element.getAttribute("aria-hidden") !== "true" && !["checkbox", "radio"].includes((element as HTMLInputElement).type)) {
          if (!classes.includes("w-full") || !classes.includes("min-w-0")) offenders.push(`${element.tagName}#${element.id} is not w-full min-w-0`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("long ids and names wrap, grids start as one column, and every step root can shrink", async () => {
    await atPhone("knowledge");
    for (const section of sections()) expect(section.className).toContain("min-w-0");
    const [knowledge, memory] = sections();
    const id = within(knowledge).getAllByText(KB1)[0];
    expect(id.className).toContain("break-all");
    expect(within(knowledge).getByRole("heading", { name: "Product docs" }).className).toContain("break-words");
    for (const grid of [...Array.from(knowledge.querySelectorAll(".grid")), ...Array.from(memory.querySelectorAll(".grid"))]) {
      expect(grid.className).toContain("grid-cols-1");
    }
    expect(knowledge.querySelectorAll(".grid").length).toBeGreaterThan(0);
    expect(memory.querySelectorAll(".grid").length).toBeGreaterThan(0);
  });

  it("Previous and Next at the ends of the new steps are named after their neighbours", async () => {
    await atPhone("knowledge");
    expect(screen.getByTestId("stepper-previous")).toHaveTextContent("Processors");
    expect(screen.getByTestId("stepper-next")).toHaveTextContent("Memory");
    fireEvent.click(screen.getByTestId("stepper-next"));
    expect(screen.getByTestId("stepper-previous")).toHaveTextContent("Knowledge");
    // AC.13: Output (step 8) follows Memory.
    expect(screen.getByTestId("stepper-next")).toHaveTextContent("Output");
  });
});

describe("accessibility", () => {
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
  const theme = (name: string) => {
    document.documentElement.classList.toggle("dark", name === "dark");
    document.documentElement.setAttribute("data-theme", name);
  };
  afterEach(() => {
    document.documentElement.classList.remove("dark");
    document.documentElement.removeAttribute("data-theme");
  });
  const levels = () => Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6")).map((h) => Number(h.tagName.slice(1)));

  it.each(["light", "dark"])("both steps with everything on have no axe violations in the %s theme", async (name) => {
    theme(name);
    seedStored(FULL);
    await renderEditor("knowledge", <Page />);
    await screen.findByRole("heading", { name: "Product docs" });
    await expectNoAxeViolations(document.body);
    goTo(/^Memory/);
    await waitFor(() => expect(optionsOf(byId("memory-observational-provider")).length).toBeGreaterThan(1));
    await expectNoAxeViolations(document.body);
    expect(levels().filter((l) => l === 1)).toHaveLength(1);
    expect(levels().every((l, i, all) => l <= (all[i - 1] ?? 0) + 1)).toBe(true);
  }, AXE_TIMEOUT_MS);

  it.each(["light", "dark"])("both steps showing their problems, models outside the allowlist and empty lists have no axe violations in the %s theme", async (name) => {
    theme(name);
    seedStored({
      ...FULL,
      knowledge_bases: [{ id: KB1, top_k: 8 }],
      memory: { working: { scope: "resource", template: "x", read_only: false }, observational: { model: { provider: "test", name: "gone" } }, semantic_recall: { embedding_model: "other:gone" } },
    } as unknown as AgentSpec);
    await renderEditor("knowledge", <Page />);
    await screen.findByRole("heading", { name: "Product docs" });
    set("kb-0-top_k", "50");
    set("kb-0-filter", '{"a": ');
    await expectNoAxeViolations(document.body);
    goTo(/^Memory/);
    await waitFor(() => expect(byId("memory-observational-model")).toHaveAttribute("aria-invalid", "true"));
    set("memory-working-template", "   ");
    set("memory-observational-observe_after_tokens", "5");
    await expectNoAxeViolations(document.body);
    expect(navButton(/^Memory/)).toHaveTextContent("2 problems");
  }, AXE_TIMEOUT_MS);

  it.each(["light", "dark"])("memory off and no knowledge bases have no axe violations in the %s theme", async (name) => {
    theme(name);
    vi.mocked(listKnowledgeBases).mockResolvedValue([]);
    await renderEditor("knowledge", <Page />);
    await screen.findByTestId("knowledge-empty");
    await expectNoAxeViolations(document.body);
    goTo(/^Memory/);
    await act(async () => undefined);
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("Save blocked by a field on the new steps", () => {
  // The platform offers no model: turning a feature on leaves its model select empty (nothing to preselect).
  async function emptySelect(feature: "observational" | "recall") {
    vi.mocked(fetchModels).mockResolvedValue([]);
    vi.mocked(listEmbeddingModels).mockResolvedValue([]);
    await renderEditor("memory");
    click("memory-enabled");
    click(`memory-${feature}-enabled`);
    await act(async () => undefined);
    goTo(/^Identity/);
    fireEvent.click(saveButton());
  }
  const FIELD = { observational: "memory-observational-model", recall: "memory-recall-embedding_model" } as const;

  it.each(["observational", "recall"] as const)("an empty %s model select opens Memory, marks the step and sends nothing", async (feature) => {
    await emptySelect(feature);
    await waitFor(() => expect(region("Memory")).toBeVisible());
    expect(navButton(/^Memory/)).toHaveAttribute("aria-current", "step");
    expect(navButton(/^Memory/)).toHaveTextContent("1 problem");
    expect(byId<HTMLSelectElement>(FIELD[feature])).toHaveValue("");
    expect(api.agents.update).not.toHaveBeenCalled();
  });

  it("control: an invalid number input opens its step and takes focus", async () => {
    seedStored({ ...PLAIN, knowledge_bases: [{ id: KB1, top_k: 8 }] } as unknown as AgentSpec);
    await renderEditor("knowledge");
    await screen.findByRole("heading", { name: "Product docs" });
    set("kb-0-top_k", "50");
    goTo(/^Identity/);
    fireEvent.click(saveButton());
    await waitFor(() => expect(region("Knowledge")).toBeVisible());
    await waitFor(() => expect(document.activeElement).toBe(byId("kb-0-top_k")));
    expect(api.agents.update).not.toHaveBeenCalled();
  });

  // AgentEditor.handleSubmit focuses a blocked select too (was a known gap, fixed after review).
  it.each(["observational", "recall"] as const)("an empty %s model select takes focus when Save is blocked", async (feature) => {
    await emptySelect(feature);
    await waitFor(() => expect(region("Memory")).toBeVisible());
    await waitFor(() => expect(document.activeElement).toBe(byId(FIELD[feature])));
  });
});

// Review fix (blocking): a cleared field with a default must save as the API's dump of it, so the
// draft is clean after the save and Publish unlocks. The update mock below answers with the API's
// real dump (eap_schemas `AgentSpec.model_validate(...).model_dump(mode="json")` of the stored spec
// with that field removed, generated by scratchpad/cleared_dumps.py), not with an echo of the body.
const STORED_NON_DEFAULT = "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":6,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":2,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}";
const CLEARED_DUMPS: Record<string, string> = {
  "kb-0-top_k":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":8,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":6,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":2,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
  "kb-0-min_score":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":6,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":2,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
  "memory-history-last_turns":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":20,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":6,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":2,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
  "memory-history-max_tokens":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":6,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":2,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
  "memory-observational-observe_after_tokens":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":12000,\"keep_recent_turns\":6,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":2,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
  "memory-observational-keep_recent_turns":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":4,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":2,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
  "memory-observational-reflect_after_tokens":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":6,\"reflect_after_tokens\":8000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":2,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
  "memory-recall-top_k":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":6,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":4,\"message_range\":2,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
  "memory-recall-message_range":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":6,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":1,\"scope\":\"resource\",\"min_score\":0.5},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
  "memory-recall-min_score":
    "{\"spec_version\":1,\"name\":\"Support bot\",\"instructions\":\"Help people\",\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"tools\":[],\"model_settings\":{\"temperature\":null,\"max_tokens\":null},\"knowledge_bases\":[{\"id\":\"11111111-1111-4111-8111-111111111111\",\"top_k\":5,\"min_score\":0.3,\"filter\":{\"lang\":\"en\",\"tag\":[\"faq\",\"guide\"]}},{\"id\":\"22222222-2222-4222-8222-222222222222\",\"top_k\":8}],\"memory\":{\"history\":{\"last_turns\":12,\"max_tokens\":4000},\"working\":{\"scope\":\"conversation\",\"json_schema\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"goals\":{\"type\":\"array\",\"items\":{\"type\":\"string\"}}}},\"read_only\":false},\"observational\":{\"model\":{\"provider\":\"test\",\"name\":\"echo\"},\"observe_after_tokens\":20000,\"keep_recent_turns\":6,\"reflect_after_tokens\":9000},\"semantic_recall\":{\"embedding_model\":\"test:hash\",\"top_k\":6,\"message_range\":2,\"scope\":\"resource\"},\"redact\":{\"kinds\":[\"email\",\"phone\"],\"action\":\"redact\"}}}",
};

describe("a cleared field saves as the API's dump: clean after save, Publish enabled", () => {
  it.each(Object.keys(CLEARED_DUMPS))("%s", async (field) => {
    seedStored(JSON.parse(STORED_NON_DEFAULT) as AgentSpec);
    vi.mocked(api.agents.update).mockImplementation(async () => {
      stored = JSON.parse(CLEARED_DUMPS[field]) as AgentSpec;
      return agentWith(stored) as never;
    });
    await renderEditor(field.startsWith("kb-") ? "knowledge" : "memory");
    if (field.startsWith("kb-")) await screen.findByRole("heading", { name: "Product docs" });
    expect(publishButton()).toBeEnabled();
    set(field, "");
    expect(byId<HTMLInputElement>(field).value).toBe("");
    expect(screen.getByText("Save the draft before publishing")).toBeInTheDocument();
    await save();
    expect(JSON.stringify(sent())).toBe(CLEARED_DUMPS[field]);
    expect(screen.queryByText("Save the draft before publishing")).toBeNull();
    expect(publishButton()).toBeEnabled();
    expect(unload()).toBe(false);
    // The cleared field stays empty (its placeholder names the default).
    expect(byId<HTMLInputElement>(field).value).toBe("");
  });
});
