import { describe, expect, it } from "vitest";
import type { AgentSpec } from "@/lib/api";
import { compareParam, parseCompareParam } from "@/lib/routes/agents";
import { mainCardWidth } from "@/components/frame/frameGeometry";
import { CORE_STEP_IDS, stepForErrorMessage, stepStatus } from "../specSections";
import { checkExtraKeys, draftDiffers, draftFromSpec, draftToSpec, numberProblems } from "./editorDraft";
import { EDITOR_COLUMNS_FIT_PX, EDITOR_NAV_FIT_PX, editorLayout } from "./editorFit";
import { EDITOR_STEPS, FIRST_STEP_ID, editorStep, missingFields, neighbourSteps, normalizeStepId, stepForError } from "./registry";
import { guardedHref } from "./useUnsavedChangesGuard";

// A stored spec in the server's dump order, with keys the editor has no field for.
const stored = {
  spec_version: 1,
  name: "Support bot",
  model: { provider: "test", name: "echo", extra_model_key: "kept" },
  instructions: "Help {{context.tier}}",
  description: "Answers support questions",
  tools: ["clock.now"],
  model_settings: { temperature: 0.2, max_tokens: null, top_p: 0.9 },
  mcp_tools: [{ server: "docs", tool: "search" }],
  context_fields: [{ name: "tier", type: "string", required: false, description: "", max_length: null, values: [] }],
  memory: { history: { turns: 20 } },
} as unknown as AgentSpec;

describe("editor draft", () => {
  it("an untouched draft saves byte-identical JSON: key order and unknown keys kept", () => {
    const saved = draftToSpec(draftFromSpec(stored), stored);
    expect(JSON.stringify(saved)).toBe(JSON.stringify(stored));
    expect(draftDiffers(draftFromSpec(stored), stored)).toBe(false);
  });

  it("an edit changes only its key and marks the draft dirty", () => {
    const draft = { ...draftFromSpec(stored), name: "Support bot 2" };
    const saved = draftToSpec(draft, stored) as unknown as Record<string, unknown>;
    expect(saved.name).toBe("Support bot 2");
    expect(Object.keys(saved)).toEqual(Object.keys(stored));
    expect(draftDiffers(draft, stored)).toBe(true);
  });

  it("extra sets and removes top-level keys (later steps own them)", () => {
    const draft = { ...draftFromSpec(stored), extra: { memory: undefined, knowledge: { bases: ["kb1"] } } };
    const saved = draftToSpec(draft, stored) as unknown as Record<string, unknown>;
    expect("memory" in saved).toBe(false);
    expect(saved.knowledge).toEqual({ bases: ["kb1"] });
    expect(Object.keys(saved).at(-1)).toBe("knowledge");
  });

  it("a new agent's spec has the editor's key order and no empty sections", () => {
    const draft = { ...draftFromSpec(undefined), name: "A", instructions: "Hi", provider: "test", modelName: "echo" };
    expect(Object.keys(draftToSpec(draft))).toEqual(["spec_version", "name", "instructions", "model", "tools", "model_settings"]);
  });

  it("lists blocking fields from the steps' missing(), in step order", () => {
    const empty = draftFromSpec(undefined);
    expect(missingFields(empty, draftToSpec(empty)).map((m) => `${m.step}:${m.fieldId}`)).toEqual([
      "identity:name",
      "identity:provider",
      "identity:modelName",
      "instructions:instructions",
    ]);
    expect(missingFields(draftFromSpec(stored), stored)).toEqual([]);
    const bad = { ...draftFromSpec(stored), temperature: "2.5", maxTokens: "1.5" };
    expect(missingFields(bad, stored).map((m) => m.fieldId)).toEqual(["temperature", "maxTokens"]);
  });

  it("checks the number fields against the API's limits, without truncating", () => {
    expect(numberProblems({ temperature: "", maxTokens: "" })).toEqual({});
    expect(numberProblems({ temperature: "0", maxTokens: "1" })).toEqual({});
    expect(numberProblems({ temperature: "2", maxTokens: "4096" })).toEqual({});
    expect(numberProblems({ temperature: "-0.1", maxTokens: "0" })).toEqual({
      temperature: "Temperature must be a number from 0 to 2",
      maxTokens: "Max tokens must be a whole number of at least 1",
    });
    expect(numberProblems({ temperature: "2.01", maxTokens: "1.5" }).maxTokens).toBeDefined();
    const draft = { ...draftFromSpec(stored), maxTokens: "1.5" };
    expect((draftToSpec(draft, stored).model_settings as { max_tokens: number }).max_tokens).toBe(1.5);
  });

  it("extra may not set keys the form owns: throws outside production, and draftToSpec ignores them", () => {
    expect(() => checkExtraKeys({ tools: ["x"], memory: {} })).toThrow(/tools/);
    expect(checkExtraKeys({ memory: {} })).toEqual({ memory: {} });
    const draft = { ...draftFromSpec(stored), extra: { model_settings: { temperature: 9 } } };
    expect(JSON.stringify(draftToSpec(draft, stored))).toBe(JSON.stringify(stored));
  });
});

describe("step registry", () => {
  it("has the five built-in steps in order, then Knowledge and Memory (RM.17) and Output (AC.13), with the ids the Config panel links to", () => {
    expect(EDITOR_STEPS.map((step) => step.id)).toEqual([...CORE_STEP_IDS, "knowledge", "memory", "output"]);
    expect(EDITOR_STEPS.map((step) => step.label)).toEqual(["Identity and model", "Instructions", "Tools", "Request context", "Processors", "Knowledge", "Memory", "Output"]);
    expect(FIRST_STEP_ID).toBe("identity");
    expect(editorStep("request-context")?.label).toBe("Request context");
  });

  it("normalizes unknown or missing step ids to the first step", () => {
    expect(normalizeStepId("tools")).toBe("tools");
    expect(normalizeStepId("nope")).toBe("identity");
    expect(normalizeStepId(null)).toBe("identity");
  });

  it("names the neighbours for Previous and Next", () => {
    expect(neighbourSteps("identity")).toEqual({ previous: null, next: EDITOR_STEPS[1] });
    expect(neighbourSteps("tools").previous?.label).toBe("Instructions");
    expect(neighbourSteps("tools").next?.label).toBe("Request context");
    expect(neighbourSteps("processors").next?.label).toBe("Knowledge");
    expect(neighbourSteps("memory").next?.label).toBe("Output");
    expect(neighbourSteps("output").next).toBeNull();
  });

  // [message, built-in step (stepForErrorMessage), registry step (stepForError, with RM.17's steps)]
  it.each([
    ["input_processors[1].config.patterns[0]: invalid RE2 pattern", "processors", "processors"],
    ["processor model 'x:y' is not allowed on this platform", "processors", "processors"],
    ["instructions: placeholder {{context.zz}} names no context field", "instructions", "instructions"],
    ["context_fields[0].name: duplicate", "request-context", "request-context"],
    ["MCP server 'x' is disabled", "tools", "tools"],
    ["model test:gone is not in the allowlist", "identity", "identity"],
    ["Model test:gone is not in the allowlist", "identity", "identity"],
    ["model_settings.temperature: must be at most 2", "identity", "identity"],
    ["mcp_tools[0]: unknown server", "tools", "tools"],
    // A later step's message that mentions a tool or an MCP server is not claimed by Tools.
    ["knowledge.bases[0]: MCP server 'x' is disabled", null, null],
    ["memory: tool 'clock.now' cannot write memory", null, "memory"],
    ["Invalid input", null, null],
  ])("maps the error %j to built-in step %s and registry step %s", (message, core, step) => {
    expect(stepForErrorMessage(message)).toBe(core);
    expect(stepForError(message)).toBe(step);
  });
});

describe("step status", () => {
  it("derives status lines and validity from the spec", () => {
    expect(stepStatus("identity", stored)).toEqual({ text: "test:echo", valid: true });
    expect(stepStatus("identity", { ...stored, name: "" })).toEqual({ text: "Required", valid: false });
    expect(stepStatus("instructions", { ...stored, instructions: " " })).toEqual({ text: "Required", valid: false });
    expect(stepStatus("instructions", stored)).toEqual({ text: "2 words", valid: true });
    expect(stepStatus("instructions", { ...stored, instructions: "Hi {{context.nope}}" })).toEqual({ text: "1 problem", valid: false });
    expect(stepStatus("tools", stored)).toEqual({ text: "2 tools", valid: true });
    expect(stepStatus("tools", { ...stored, tools: [], mcp_tools: undefined })).toEqual({ text: "None", valid: true });
    expect(stepStatus("request-context", stored)).toEqual({ text: "1 field", valid: true });
    expect(stepStatus("processors", stored)).toEqual({ text: "None", valid: true });
    expect(stepStatus("memory", stored)).toBeNull();
  });
});

describe("editor fit rule", () => {
  const wide = (viewport: number, panelWidth: number | null = null, sidebarCollapsed = false) =>
    editorLayout({ frame: { viewport, sidebarCollapsed }, isWide: viewport >= 1024, panelWidth });

  it("three columns at 1280 px and up with Config closed", () => {
    expect(mainCardWidth({ viewport: 1280, sidebarCollapsed: false }, null)).toBeGreaterThanOrEqual(EDITOR_COLUMNS_FIT_PX);
    expect(wide(1280)).toBe("columns");
    expect(wide(1440)).toBe("columns");
  });

  it("versions move to the Sheet where Config leaves too little room, and at 1024 px", () => {
    expect(wide(1440, 400)).toBe("nav");
    expect(wide(1024)).toBe("nav");
    // The rail gives the room back.
    expect(wide(1180, null, true)).toBe("columns");
  });

  it("the nav becomes a select below 768 px or where nav + step do not fit", () => {
    expect(wide(767)).toBe("compact");
    expect(wide(400)).toBe("compact");
    expect(wide(375)).toBe("compact");
    expect(editorLayout({ frame: { viewport: 900, sidebarCollapsed: false }, isWide: false, panelWidth: null })).toBe("nav");
    expect(EDITOR_NAV_FIT_PX).toBeLessThan(EDITOR_COLUMNS_FIT_PX);
  });
});

describe("compare param", () => {
  it("round-trips a..b and rejects malformed values", () => {
    expect(compareParam(3, "draft")).toBe("3..draft");
    expect(parseCompareParam("3..draft")).toEqual({ base: 3, target: "draft" });
    expect(parseCompareParam("1..3")).toEqual({ base: 1, target: 3 });
    for (const bad of [null, "", "3", "3..3", "0..2", "a..b", "1..2..3", "-1..2", "draft..draft"]) expect(parseCompareParam(bad)).toBeNull();
  });
});

describe("unsaved-changes guard: which clicks it holds", () => {
  const origin = "http://studio.test";
  const click = (href: string, extra: Partial<MouseEvent> & { targetAttr?: string } = {}) => {
    const a = document.createElement("a");
    a.setAttribute("href", href);
    if (extra.targetAttr) a.setAttribute("target", extra.targetAttr);
    const span = document.createElement("span");
    a.appendChild(span);
    return { button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, defaultPrevented: false, target: span, ...extra } as MouseEvent;
  };

  it("holds a plain click on a link to another page", () => {
    expect(guardedHref(click("/agents/a1/chat?version=draft"), "/agents/a1/editor", origin)).toBe("/agents/a1/chat?version=draft");
  });

  it("never returns a target router.push would take off-site (a same-origin `//host` path)", () => {
    expect(guardedHref(click(`${origin}//evil.example/x`), "/agents/a1/editor", origin)).toBeNull();
    expect(guardedHref(click(`${origin}/agents/a2`), "/agents/a1/editor", origin)).toBe("/agents/a2");
  });

  it("lets through links within the editor, modified clicks, new tabs and other origins", () => {
    expect(guardedHref(click("/agents/a1/editor?step=tools"), "/agents/a1/editor", origin)).toBeNull();
    expect(guardedHref(click("/agents"), "/agents/a1/editor", origin)).toBe("/agents");
    expect(guardedHref(click("/agents", { metaKey: true }), "/agents/a1/editor", origin)).toBeNull();
    expect(guardedHref(click("/agents", { targetAttr: "_blank" }), "/agents/a1/editor", origin)).toBeNull();
    expect(guardedHref(click("https://example.com/x"), "/agents/a1/editor", origin)).toBeNull();
    expect(guardedHref(click("/agents", { defaultPrevented: true }), "/agents/a1/editor", origin)).toBeNull();
  });
});

describe("editor layout below 1024 px (the card's gutter and border, frameGeometry.narrowMainWidth)", () => {
  const at = (viewport: number) => editorLayout({ frame: { viewport, sidebarCollapsed: false }, isWide: false, panelWidth: null });

  it.each([916, 920, 933])("%i px: the card is 18 px narrower than the window, so no columns (was squeezed)", (viewport) => {
    expect(at(viewport)).toBe("nav");
  });

  it("columns from the window width whose card fits them", () => {
    expect(at(EDITOR_COLUMNS_FIT_PX + 18)).toBe("columns");
    expect(at(EDITOR_COLUMNS_FIT_PX + 17)).toBe("nav");
  });
});
