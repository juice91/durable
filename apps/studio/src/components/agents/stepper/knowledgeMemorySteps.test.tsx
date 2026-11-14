// RM.17: the editor's Knowledge and Memory steps: config round trip, memory null vs object,
// knowledge base attach and detach, filters, inline and server errors, phone width, axe.
import { readableError } from "@/components/agents/readableError";
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/agents/a1/editor",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({ id: "a1" }),
}));
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(), listEmbeddingModels: vi.fn() }));
vi.mock("@/lib/tools", () => ({ listTools: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(() => Promise.resolve([])), test: vi.fn() } };
});
vi.mock("@/components/agents/specSections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/agents/specSections")>();
  return { ...actual, fetchProcessorCatalog: vi.fn(), fetchModels: vi.fn() };
});

import type { AgentSpec } from "@/lib/api";
import { listEmbeddingModels, listKnowledgeBases, type KnowledgeBase } from "@/lib/knowledge";
import { fetchModels, fetchProcessorCatalog } from "@/components/agents/specSections";
import { modelsFixture, processorCatalogFixture } from "@/components/agents/specSections.fixtures";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { AgentEditor } from "../AgentEditor";
import type { EditorLayout } from "./editorFit";

const KB1 = "11111111-1111-4111-8111-111111111111";
const KB2 = "22222222-2222-4222-8222-222222222222";
const GONE = "33333333-3333-4333-8333-333333333333";
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

/** A stored spec as the API dumps it, with knowledge and every memory feature. */
const FULL = {
  ...PLAIN,
  knowledge_bases: [{ id: KB1, top_k: 5, min_score: 0.3, filter: { lang: "en", tag: ["faq", "guide"] } }],
  memory: {
    history: { last_turns: 12, max_tokens: 4000 },
    working: { scope: "conversation", json_schema: { type: "object", properties: { name: { type: "string" } } }, read_only: false },
    observational: { model: { provider: "test", name: "echo" }, observe_after_tokens: 12000, keep_recent_turns: 4, reflect_after_tokens: 8000 },
    semantic_recall: { embedding_model: "test:hash", top_k: 4, message_range: 1, scope: "resource", min_score: 0.5 },
    redact: { kinds: ["email", "phone"], action: "redact" },
  },
} as unknown as AgentSpec;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listKnowledgeBases).mockResolvedValue(BASES);
  vi.mocked(listEmbeddingModels).mockResolvedValue(EMBEDDINGS);
  vi.mocked(fetchModels).mockResolvedValue(modelsFixture);
  vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
});

function Host({
  initial,
  onSubmit,
  serverError = null,
  start = "knowledge",
  layout = "nav",
}: {
  initial: AgentSpec;
  onSubmit: (spec: AgentSpec) => Promise<void>;
  serverError?: string | null;
  start?: string;
  layout?: EditorLayout;
}) {
  const [step, setStep] = React.useState(start);
  return <AgentEditor initialData={initial} onSubmit={onSubmit} submitLabel="Save draft" serverError={serverError} stepper={{ activeStep: step, onStepChange: setStep, layout }} />;
}

function setup(initial: AgentSpec, props: Partial<React.ComponentProps<typeof Host>> = {}) {
  const saved: AgentSpec[] = [];
  const onSubmit = vi.fn(async (spec: AgentSpec) => {
    saved.push(spec);
  });
  const view = render(<Host initial={initial} onSubmit={onSubmit} {...props} />);
  return { ...view, saved, onSubmit };
}

const stepNav = () => screen.getByRole("navigation", { name: "Editor steps" });
const navButton = (label: RegExp) => within(stepNav()).getByRole("button", { name: label });
const save = async () => {
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  await act(async () => undefined);
};
const goTo = (label: RegExp) => fireEvent.click(navButton(label));

describe("round trip and hash stability", () => {
  it("an untouched agent without knowledge or memory saves the same JSON, with neither key", async () => {
    const { saved } = setup(PLAIN);
    await screen.findByText("Product docs");
    goTo(/^Memory/);
    await save();
    expect(JSON.stringify(saved[0])).toBe(JSON.stringify(PLAIN));
    expect(saved[0]).not.toHaveProperty("memory");
    expect(saved[0]).not.toHaveProperty("knowledge_bases");
  });

  it("an untouched agent with knowledge and every memory feature saves byte-identical JSON", async () => {
    const { saved } = setup(FULL);
    expect(await screen.findByRole("heading", { name: "Product docs" })).toBeInTheDocument();
    goTo(/^Memory/);
    await waitFor(() => expect(screen.getByLabelText("Observer model")).toHaveValue("test:echo"));
    await save();
    expect(JSON.stringify(saved[0])).toBe(JSON.stringify(FULL));
  });

  it("the nav shows each step's status from the spec", async () => {
    setup(FULL);
    await act(async () => undefined);
    expect(navButton(/^Knowledge/)).toHaveTextContent("1 knowledge base");
    expect(navButton(/^Memory/)).toHaveTextContent("History 12 · Working · Observational · Recall");
    expect(navButton(/^Memory/)).toHaveAccessibleName(/complete/);
  });
});

describe("Knowledge step", () => {
  it("attaches a knowledge base with the default shape, edits settings, and detaching restores the original JSON", async () => {
    const { saved } = setup(PLAIN);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Product docs" }));
    expect(navButton(/^Knowledge/)).toHaveTextContent("1 knowledge base");
    expect(screen.getByTestId("knowledge-count")).toHaveTextContent("1 attached (max 8)");
    await save();
    expect((saved[0] as unknown as { knowledge_bases: unknown }).knowledge_bases).toEqual([{ id: KB1, top_k: 8 }]);

    fireEvent.change(screen.getByLabelText("Top K (1–20)"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Minimum score (0–1, optional)"), { target: { value: "0.4" } });
    fireEvent.change(screen.getByLabelText("Metadata filter (optional)"), { target: { value: '{"lang": "en", "tag": ["a", "b"]}' } });
    await save();
    expect(JSON.stringify((saved[1] as unknown as { knowledge_bases: unknown }).knowledge_bases)).toBe(
      JSON.stringify([{ id: KB1, top_k: 3, min_score: 0.4, filter: { lang: "en", tag: ["a", "b"] } }]),
    );

    fireEvent.click(screen.getByRole("button", { name: "Detach Product docs" }));
    expect(navButton(/^Knowledge/)).toHaveTextContent("None");
    await save();
    expect(JSON.stringify(saved[2])).toBe(JSON.stringify(PLAIN));
  });

  it("unchecking in the list detaches too; clearing the filter removes it", async () => {
    const { saved } = setup(FULL);
    fireEvent.change(await screen.findByLabelText("Metadata filter (optional)"), { target: { value: "" } });
    await save();
    expect((saved[0] as unknown as { knowledge_bases: unknown[] }).knowledge_bases).toEqual([{ id: KB1, top_k: 5, min_score: 0.3 }]);
    fireEvent.change(screen.getByLabelText("Metadata filter (optional)"), { target: { value: "{}" } });
    await save();
    expect((saved[1] as unknown as { knowledge_bases: unknown[] }).knowledge_bases).toEqual([{ id: KB1, top_k: 5, min_score: 0.3 }]);
    fireEvent.click(screen.getByRole("checkbox", { name: "Product docs" }));
    await save();
    expect(saved[2]).not.toHaveProperty("knowledge_bases");
  });

  it("an invalid filter and an out-of-range top K show inline, mark the step and block saving", async () => {
    const { onSubmit } = setup({ ...PLAIN, knowledge_bases: [{ id: KB1, top_k: 8 }] } as unknown as AgentSpec);
    const filter = await screen.findByLabelText("Metadata filter (optional)");
    fireEvent.change(filter, { target: { value: '{"lang": ' } });
    expect(filter).toHaveAccessibleDescription(/Filter is not valid JSON/);
    expect(filter).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(filter, { target: { value: '{"lang": {"x": 1}}' } });
    expect(filter).toHaveAccessibleDescription(/filter.lang: value must be a string, number or boolean/);
    fireEvent.change(screen.getByLabelText("Top K (1–20)"), { target: { value: "50" } });
    expect(screen.getByLabelText("Top K (1–20)")).toHaveAccessibleDescription(/Top K must be a whole number from 1 to 20/);
    expect(navButton(/^Knowledge/)).toHaveTextContent("2 problems");
    expect(navButton(/^Knowledge/)).toHaveAccessibleName(/needs attention/);
    goTo(/^Identity/);
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(navButton(/^Knowledge/)).toHaveAttribute("aria-current", "step");
  });

  it("an attached knowledge base that no longer exists is named and can be detached", async () => {
    setup({ ...PLAIN, knowledge_bases: [{ id: GONE, top_k: 8 }] } as unknown as AgentSpec);
    expect(await screen.findByRole("heading", { name: "Unknown knowledge base" })).toBeInTheDocument();
    expect(screen.getByText(/does not exist in this tenant/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Detach Unknown knowledge base" }));
    expect(screen.queryByTestId("knowledge-ref")).toBeNull();
  });

  it("at 8 attached the rest of the list is disabled", async () => {
    const many = Array.from({ length: 9 }, (_, i) => kb(`0000000${i}-0000-4000-8000-000000000000`, `KB ${i}`));
    vi.mocked(listKnowledgeBases).mockResolvedValue(many);
    setup({ ...PLAIN, knowledge_bases: many.slice(0, 8).map((k) => ({ id: k.id, top_k: 8 })) } as unknown as AgentSpec);
    expect(await screen.findByRole("checkbox", { name: "KB 8" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "KB 0" })).toBeEnabled();
  });

  it("empty and failed lists", async () => {
    vi.mocked(listKnowledgeBases).mockResolvedValueOnce([]);
    const first = setup(PLAIN);
    expect(await screen.findByTestId("knowledge-empty")).toHaveTextContent("No knowledge bases yet");
    expect(screen.getByRole("link", { name: "Create one on the Knowledge page" })).toHaveAttribute("href", "/knowledge");
    first.unmount();
    vi.mocked(listKnowledgeBases).mockRejectedValueOnce(new Error("boom"));
    setup(PLAIN);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load knowledge bases: boom");
  });

  it("loads the list only once the step is shown", async () => {
    setup(PLAIN, { start: "identity" });
    await act(async () => undefined);
    expect(listKnowledgeBases).not.toHaveBeenCalled();
    expect(listEmbeddingModels).not.toHaveBeenCalled();
    goTo(/^Knowledge/);
    await screen.findByText("Product docs");
    expect(listKnowledgeBases).toHaveBeenCalledTimes(1);
  });
});

describe("Memory step", () => {
  it("memory off leaves the key out; on with nothing set is {}; off again removes it (never {} or null)", async () => {
    const { saved } = setup(PLAIN, { start: "memory" });
    const toggle = screen.getByRole("checkbox", { name: "Use memory" });
    expect(toggle).not.toBeChecked();
    expect(navButton(/^Memory/)).toHaveTextContent("Off");
    fireEvent.click(toggle);
    expect(navButton(/^Memory/)).toHaveTextContent("History 20");
    await save();
    expect((saved[0] as unknown as { memory: unknown }).memory).toEqual({});
    fireEvent.click(toggle);
    await save();
    expect(saved[1]).not.toHaveProperty("memory");
    expect(JSON.stringify(saved[1])).toBe(JSON.stringify(PLAIN));
  });

  it("turns every feature on and off; each toggle adds or removes only its own key", async () => {
    const { saved } = setup(PLAIN, { start: "memory" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Use memory" }));
    await waitFor(() => expect(fetchModels).toHaveBeenCalled());
    await act(async () => undefined);
    fireEvent.change(screen.getByLabelText(/^Last turns/), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "Keep a working memory note" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Compress older turns into observations" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Recall related earlier turns" }));
    fireEvent.click(screen.getByRole("radio", { name: "Choose kinds" }));
    await save();
    expect(JSON.stringify((saved[0] as unknown as { memory: unknown }).memory)).toBe(
      JSON.stringify({
        history: { last_turns: 10 },
        working: { scope: "resource", read_only: false },
        observational: { model: { provider: "test", name: "echo" }, observe_after_tokens: 12000, keep_recent_turns: 4, reflect_after_tokens: 8000 },
        semantic_recall: { embedding_model: "test:hash", top_k: 4, message_range: 1, scope: "resource" },
        redact: { kinds: ["email", "phone", "credit_card", "iban", "ipv4", "us_ssn"], action: "redact" },
      }),
    );
    fireEvent.change(screen.getByLabelText(/^Last turns/), { target: { value: "" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "Keep a working memory note" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Compress older turns into observations" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Recall related earlier turns" }));
    fireEvent.click(screen.getByRole("radio", { name: /Same as input redaction/ }));
    await save();
    // A cleared Last turns stores the default (the API's dump of the history config), shown empty.
    expect((saved[1] as unknown as { memory: unknown }).memory).toEqual({ history: { last_turns: 20 } });
    expect(screen.getByLabelText(/^Last turns/)).toHaveValue(null);
  });

  it("the schema editor shows subset errors inline and blocks saving; a valid schema saves", async () => {
    const { saved, onSubmit } = setup(PLAIN, { start: "memory" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Use memory" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Keep a working memory note" }));
    fireEvent.click(screen.getByRole("radio", { name: /JSON schema/ }));
    const editor = screen.getByLabelText("JSON schema");
    expect(JSON.parse((editor as HTMLTextAreaElement).value)).toEqual({
      type: "object",
      properties: { name: { type: "string" }, preferences: { type: "array", items: { type: "string" } }, goals: { type: "array", items: { type: "string" } } },
    });
    fireEvent.change(editor, { target: { value: '{"type": "object", "properties": {"born": {"type": "date"}}}' } });
    expect(editor).toHaveAccessibleDescription(/born: unsupported type 'date'/);
    expect(navButton(/^Memory/)).toHaveTextContent("1 problem");
    fireEvent.change(editor, { target: { value: '{"type": ' } });
    expect(editor).toHaveAccessibleDescription(/Schema is not valid JSON/);
    fireEvent.change(editor, { target: { value: "" } });
    expect(editor).toHaveAccessibleDescription(/Enter a JSON schema/);
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.change(editor, { target: { value: '{"type": "object", "properties": {"plan": {"enum": ["free", "pro"]}}}' } });
    expect(navButton(/^Memory/)).toHaveAccessibleName(/complete/);
    await save();
    expect((saved[0] as unknown as { memory: { working: unknown } }).memory.working).toEqual({
      scope: "resource",
      json_schema: { type: "object", properties: { plan: { enum: ["free", "pro"] } } },
      read_only: false,
    });
    // Back to a template: the schema goes, an empty template is the default (left out).
    fireEvent.click(screen.getByRole("radio", { name: /Markdown template/ }));
    await save();
    expect((saved[1] as unknown as { memory: { working: unknown } }).memory.working).toEqual({ scope: "resource", read_only: false });
  });

  it("a blank template is refused inline; a template saves as typed", async () => {
    const { saved } = setup({ ...PLAIN, memory: { working: { scope: "resource", read_only: false } } } as unknown as AgentSpec, { start: "memory" });
    const template = screen.getByLabelText("Template (optional)");
    expect(template).toHaveAttribute("placeholder", "# User\n- Name:\n- Preferences:\n- Goals:");
    fireEvent.change(template, { target: { value: "   " } });
    expect(template).toHaveAccessibleDescription(/Template cannot be blank/);
    fireEvent.change(template, { target: { value: "# Customer\n- Plan:" } });
    await save();
    expect((saved[0] as unknown as { memory: { working: unknown } }).memory.working).toEqual({ scope: "resource", template: "# Customer\n- Plan:", read_only: false });
  });

  it("the observer and embedding pickers list allowlisted models only", async () => {
    setup(FULL, { start: "memory" });
    const observer = await screen.findByLabelText("Observer model");
    await waitFor(() => expect(within(observer).getAllByRole("option").length).toBeGreaterThan(1));
    const observerOptions = within(observer).getAllByRole("option").map((o) => (o as HTMLOptionElement).value).filter(Boolean);
    expect(observerOptions).toEqual(modelsFixture.map((m) => m.id));
    const embedding = screen.getByLabelText("Embedding model");
    const embeddingOptions = within(embedding).getAllByRole("option").map((o) => (o as HTMLOptionElement).value).filter(Boolean);
    expect(embeddingOptions).toEqual(["test:hash", "litellm:*"]);
    // A wildcard entry asks for the name.
    fireEvent.change(embedding, { target: { value: "litellm:*" } });
    expect(navButton(/^Memory/)).toHaveTextContent("1 problem");
    fireEvent.change(screen.getByLabelText("Embedding model: litellm model name"), { target: { value: "nomic-embed-text" } });
    expect(navButton(/^Memory/)).toHaveAccessibleName(/complete/);
  });

  it("a stored model outside the allowlist stays selected and is marked", async () => {
    const spec = { ...FULL, memory: { observational: { model: { provider: "test", name: "gone" }, observe_after_tokens: 12000, keep_recent_turns: 4, reflect_after_tokens: 8000 } } } as unknown as AgentSpec;
    setup(spec, { start: "memory" });
    const observer = await screen.findByLabelText("Observer model");
    await waitFor(() => expect(observer).toHaveAccessibleDescription(/Not in the platform allowlist/));
    expect(observer).toHaveValue("test:gone");
  });

  it("redaction: inherit names the input processor's kinds; custom needs at least one kind", async () => {
    const spec = { ...PLAIN, input_processors: [{ type: "pii_redact", config: { kinds: ["email", "iban"], action: "redact" } }], memory: {} } as unknown as AgentSpec;
    const { onSubmit } = setup(spec, { start: "memory" });
    expect(screen.getByText("Redacts email, iban (the input PII processor)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Choose kinds" }));
    for (const kind of ["email", "phone", "credit_card", "iban", "ipv4", "us_ssn"]) fireEvent.click(screen.getByRole("checkbox", { name: kind }));
    expect(screen.getByText("Pick at least one kind of personal data")).toBeInTheDocument();
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("number fields: out of range shows inline; empty stores the default and stays empty", async () => {
    const { saved } = setup(FULL, { start: "memory" });
    const keep = screen.getByLabelText("Recent turns kept");
    fireEvent.change(keep, { target: { value: "99" } });
    expect(keep).toHaveAccessibleDescription(/Recent turns kept must be a whole number from 0 to 50/);
    fireEvent.change(keep, { target: { value: "" } });
    expect(keep).toHaveAttribute("placeholder", "4");
    await save();
    // Cleared: the default is stored (as the API dumps it) while the field stays empty.
    expect((saved[0] as unknown as { memory: { observational: { keep_recent_turns: number } } }).memory.observational.keep_recent_turns).toBe(4);
    expect(keep).toHaveValue(null);
  });
});

describe("server errors open the step", () => {
  it.each([
    [`knowledge base '${KB2}' does not exist in this tenant`, "Knowledge"],
    ["memory.semantic_recall: embedding model 'litellm:x' is not allowed", "Memory"],
    ["observer model 'test:nope' is not allowed on this platform (not in the EAP_ALLOWED_MODELS allowlist)", "Memory"],
    ["Value error, working memory is invalid at a: unsupported type 'date'", "Memory"],
  ])("%s -> %s", async (message, label) => {
    const { rerender } = setup(PLAIN, { start: "identity" });
    rerender(<Host initial={PLAIN} onSubmit={async () => undefined} serverError={message} start="identity" />);
    await waitFor(() => expect(navButton(new RegExp(`^${label}`))).toHaveAttribute("aria-current", "step"));
    expect(navButton(new RegExp(`^${label}`))).toHaveTextContent("Has an error");
    const region = screen.getByRole("region", { name: label });
    // Shown in plain words (follow-up 1b); routed on the raw message.
    expect(within(region).getByTestId("agent-save-error")).toHaveTextContent(readableError(message));
  });
});

describe("phone width and accessibility", () => {
  it("at the compact layout the select lists both steps and no step field has a fixed width", async () => {
    const { container } = setup(FULL, { start: "memory", layout: "compact" });
    await act(async () => undefined);
    const select = screen.getByTestId("stepper-select") as HTMLSelectElement;
    expect(Array.from(select.options).slice(-2).map((o) => o.textContent)).toEqual([
      "6. Knowledge — 1 knowledge base",
      "7. Memory — History 12 · Working · Observational · Recall",
    ]);
    expect(screen.getByText("Step 7 of 7")).toBeInTheDocument();
    const offenders: string[] = [];
    for (const step of ["knowledge", "memory"]) {
      container.querySelector(`section[data-step="${step}"]`)!.querySelectorAll("*").forEach((element) => {
        const cls = element.getAttribute("class") ?? "";
        if (/(^|\s)(w|min-w)-\[\d+px\]/.test(cls) || /(^|\s)w-(64|72|80|96)(\s|$)/.test(cls)) offenders.push(`${element.tagName} ${cls}`);
      });
    }
    expect(offenders).toEqual([]);
  });

  it.each(["knowledge", "memory"])("the %s step with everything on has no axe violations", async (step) => {
    const { container } = setup(FULL, { start: step });
    if (step === "knowledge") await screen.findByRole("heading", { name: "Product docs" });
    else await waitFor(() => expect(screen.getByLabelText("Observer model")).toHaveValue("test:echo"));
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("review fixes", () => {
  const withKinds = (kinds: string[]) => ({ ...PLAIN, memory: { redact: { kinds, action: "redact" } } }) as unknown as AgentSpec;

  it("an unknown stored PII kind warns but does not block an untouched agent's save", async () => {
    const spec = withKinds(["email", "passport"]);
    const { saved } = setup(spec, { start: "identity" });
    expect(navButton(/^Memory/)).toHaveTextContent("1 problem");
    await save();
    expect(JSON.stringify(saved[0])).toBe(JSON.stringify(spec));
  });

  it("toggling a kind keeps an unknown stored kind, which then blocks until removed", async () => {
    const { saved, onSubmit } = setup(withKinds(["email", "passport"]), { start: "memory" });
    await act(async () => undefined);
    expect(screen.getByRole("checkbox", { name: /passport/ })).toBeChecked();
    expect(screen.getByText("(unknown)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "phone" }));
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById("memory-redact-kinds")));
    fireEvent.click(screen.getByRole("checkbox", { name: /passport/ }));
    await save();
    expect((saved[0] as unknown as { memory: { redact: unknown } }).memory.redact).toEqual({ kinds: ["email", "phone"], action: "redact" });
  });

  it("the kinds come from the processor catalog", async () => {
    const catalog = structuredClone(processorCatalogFixture);
    const kinds = (catalog.items.find((i) => i.id === "pii_redact")!.config_schema.properties!.kinds.items as { enum: string[] }).enum;
    kinds.push("passport");
    vi.mocked(fetchProcessorCatalog).mockResolvedValue(catalog);
    setup(withKinds(["email"]), { start: "memory" });
    expect(await screen.findByRole("checkbox", { name: "passport" })).not.toBeChecked();
    expect(screen.queryByText("(unknown)")).toBeNull();
  });

  it("a number the browser cannot parse (badInput) is flagged, not replaced by the default", async () => {
    const { onSubmit } = setup({ ...PLAIN, knowledge_bases: [{ id: KB1, top_k: 5 }] } as unknown as AgentSpec);
    const field = await screen.findByLabelText("Top K (1–20)");
    Object.defineProperty(field, "validity", { configurable: true, value: { badInput: true } });
    fireEvent.change(field, { target: { value: "" } });
    expect(field).toHaveAccessibleDescription(/Top K must be a whole number from 1 to 20/);
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("every problem's focus target exists and takes focus", async () => {
    const nine = Array.from({ length: 9 }, (_, i) => kb(`0000000${i}-0000-4000-8000-000000000000`, `KB ${i}`));
    vi.mocked(listKnowledgeBases).mockResolvedValue(nine);
    const spec = {
      ...PLAIN,
      knowledge_bases: [...nine.map((k) => ({ id: k.id, top_k: 8 })), { id: "not-a-uuid", top_k: 8 }],
      memory: {
        working: { scope: "team", template: "# x", json_schema: { type: "object" }, read_only: false },
        semantic_recall: { embedding_model: "test:hash", top_k: 4, message_range: 1, scope: "tenant" },
      },
    } as unknown as AgentSpec;
    setup(spec);
    await screen.findAllByText("KB 0");
    for (const id of ["kb-list", "kb-9-id", "memory-working-format", "memory-working-scope", "memory-recall-scope"]) {
      const element = document.getElementById(id);
      expect(element, id).not.toBeNull();
      element!.focus();
      expect(document.activeElement, id).toBe(element);
    }
  });
});

describe("server errors in plain words (follow-up 1b)", () => {
  it("a pydantic 'Value error' on the name opens Identity without the prefix", async () => {
    const { rerender } = setup(PLAIN, { start: "memory" });
    rerender(<Host initial={PLAIN} onSubmit={async () => undefined} serverError="Value error, name cannot be empty" start="memory" />);
    await waitFor(() => expect(navButton(/^Identity/)).toHaveAttribute("aria-current", "step"));
    const alert = within(screen.getByRole("region", { name: /^Identity/ })).getByTestId("agent-save-error");
    expect(alert).toHaveTextContent(/^Name cannot be empty$/);
    expect(alert.textContent).not.toMatch(/Value error/);
  });
});
