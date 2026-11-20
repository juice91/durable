// P0.16's Request Context page acceptance tests, migrated to the chat composer's popover (SL.10). The
// request context module is real: GET /request-context and POST /request-context/validate go through
// a stubbed fetch, and presets go to the real localStorage.
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatTab } from "@/components/agentPage/ChatTab";
import { api } from "@/lib/api";
import { listConversations } from "@/lib/conversations";
import { fetchModels } from "@/lib/metrics";
import { credentialScope, listPresets, presetsKey, savePreset } from "@/lib/requestContext";
import { routes } from "@/lib/routes";
import { getSelectOption } from "@/test/select";

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

const TOKEN = "Bearer s3cr3t-credential";
const spec = { spec_version: 1 as const, name: "Support", instructions: "", model: { provider: "test", name: "echo" }, tools: [] };
const AGENT = { id: "agent-1", name: "Support", draft_spec: spec, latest_version: 2 };
const VERSIONS = [1, 2].map((version) => ({ agent_id: "agent-1", version, spec, spec_hash: `h${version}`, created_at: "2026-11-14T00:00:00Z", created_by: "dev" }));
const INFO = {
  agent_id: "agent-1", version: 2, draft: false,
  fields: [
    { name: "tier", type: "enum", required: true, description: "", max_length: null, values: ["gold", "silver"] },
    { name: "seats", type: "integer", required: false, description: "", max_length: null, values: [] },
    { name: "note", type: "string", required: false, description: "", max_length: 5, values: [] },
  ],
  schema: {
    type: "object", required: ["tier"],
    properties: { tier: { type: "string", enum: ["gold", "silver"] }, seats: { type: "integer" }, note: { type: "string", maxLength: 5 } },
  },
};

type Reply = { status: number; body: unknown };
let validate: (body: Record<string, unknown>) => Reply | Promise<Reply>;
let validateBodies: Record<string, unknown>[] = [];

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", TOKEN);
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", routes.agentChat("agent-1"));
  vi.mocked(api.agents.get).mockResolvedValue(AGENT);
  vi.mocked(api.versions.list).mockResolvedValue(VERSIONS);
  vi.mocked(listConversations).mockResolvedValue({ items: [], next_before: null } as never);
  vi.mocked(fetchModels).mockResolvedValue([]);
  validateBodies = [];
  validate = (body) => ({ status: 200, body: { valid: true, request_context: body.request_context } });
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input);
    let reply: Reply = { status: 404, body: { detail: "nf" } };
    if (url.pathname.endsWith("/request-context/validate")) {
      const body = JSON.parse(String(init?.body));
      validateBodies.push(body.request_context);
      reply = await validate(body);
    } else if (url.pathname.endsWith("/request-context")) reply = { status: 200, body: INFO };
    return new Response(JSON.stringify(reply.body), { status: reply.status, headers: { "Content-Type": "application/json" } });
  }));
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const status = () => screen.getByTestId("context-validation-status");
const SLOW = { timeout: 3000 };

async function openContext() {
  render(<ChatTab agentId="agent-1" />);
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

describe("Request context popover acceptance (P0.16 page acceptance, SL.10)", () => {
  it("shows each field's own server error next to its input", async () => {
    validate = () => ({
      status: 422,
      body: { detail: [
        { loc: ["body", "request_context", "seats"], msg: "must be an integer", type: "int_parsing" },
        { loc: ["body", "request_context", "note"], msg: "is too long", type: "string_too_long" },
        { loc: ["body", "request_context", "tier"], msg: "is required", type: "missing" },
      ] },
    });
    await openContext();
    expect(await screen.findByTestId("field-error-seats", {}, SLOW)).toHaveTextContent("must be an integer");
    expect(screen.getByTestId("field-error-note")).toHaveTextContent("is too long");
    expect(screen.getByTestId("field-error-tier")).toHaveTextContent("is required");
    expect(screen.getByRole("button", { name: "Save as…" })).toBeDisabled();
  });

  it("live validation drops a stale response that arrives after a newer one", async () => {
    let releaseFirst: (r: Reply) => void = () => {};
    let n = 0;
    validate = (body) => {
      n += 1;
      if (n === 1) return new Promise<Reply>((r) => { releaseFirst = r; });
      return { status: 200, body: { valid: true, request_context: body.request_context } };
    };
    await openContext();
    const tier = await screen.findByLabelText(/Tier/);
    await waitFor(() => expect(validateBodies).toHaveLength(1), SLOW);
    fireEvent.change(tier, { target: { value: "gold" } });
    await waitFor(() => expect(status()).toHaveAttribute("data-state", "valid"), SLOW);
    expect(validateBodies.at(-1)).toEqual({ tier: "gold" });
    await act(async () => {
      releaseFirst({ status: 422, body: { detail: [{ loc: ["body", "request_context", "tier"], msg: "is required", type: "missing" }] } });
    });
    expect(status()).toHaveAttribute("data-state", "valid");
    expect(screen.queryByTestId("field-error-tier")).toBeNull();
    expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled();
  });

  it("a transport failure is an error state that cannot be saved", async () => {
    validate = () => ({ status: 500, body: { detail: "database down" } });
    await openContext();
    await screen.findByLabelText(/Tier/);
    await waitFor(() => expect(status()).toHaveAttribute("data-state", "error"), SLOW);
    expect(screen.getByTestId("context-validation-message")).toHaveTextContent("database down");
    expect(screen.getByRole("button", { name: "Save as…" })).toBeDisabled();
  });

  it("saving stores the accepted context under a credential-scoped key, not the token", async () => {
    await openContext();
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), SLOW);
    await saveAs("Gold");
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Saved preset “Gold”.");
    const presetKeys = Object.keys(window.localStorage).filter((key) => key.includes("equestContext"));
    expect(presetKeys).toEqual([`studio.requestContextPresets.v1.${credentialScope()}:agent-1`]);
    expect(presetKeys[0]).toBe(presetsKey("agent-1"));
    expect(presetKeys[0]).toMatch(/^studio\.requestContextPresets\.v1\.c[0-9a-f]{16}:agent-1$/);
    for (const key of Object.keys(window.localStorage)) {
      expect(key).not.toContain("s3cr3t");
      expect(window.localStorage.getItem(key)).not.toContain("s3cr3t");
    }
    expect(listPresets("agent-1")).toEqual([expect.objectContaining({ name: "Gold", value: { tier: "gold" }, version: null })]);
    expect(screen.getByTestId("saved-context-storage-note")).toHaveTextContent("unencrypted");
  });

  it("another credential never sees the presets", async () => {
    savePreset("agent-1", "Silver", { tier: "silver" }, 2);
    expect(window.localStorage.getItem(presetsKey("agent-1"))).not.toBeNull();
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer someone-else");
    expect(listPresets("agent-1")).toEqual([]);
    const dialog = await openContext();
    expect(await screen.findByLabelText(/Tier/)).toHaveValue("");
    expect(within(dialog).getByRole("combobox", { name: "Preset" })).toBeDisabled();
    expect(getSelectOption("No presets yet", dialog)).toBeTruthy();
  });

  it("a storage that throws on save is reported, and nothing is claimed saved", async () => {
    await openContext();
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save as…" })).toBeEnabled(), SLOW);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    await saveAs("Gold");
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Browser storage is unavailable");
    expect(getSelectOption("No presets yet")).toBeTruthy();
  });

  it("a storage that throws on read still renders the form", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    await openContext();
    expect(await screen.findByLabelText(/Tier/)).toHaveValue("");
  });

  it("Delete removes the preset; a storage failure on delete is reported", async () => {
    savePreset("agent-1", "Gold", { tier: "gold" }, 2);
    await openContext();
    await screen.findByLabelText(/Tier/);
    expect(screen.getByRole("combobox", { name: "Preset" })).toHaveValue("Gold");
    vi.spyOn(Storage.prototype, "setItem").mockImplementationOnce(() => {
      throw new Error("SecurityError");
    });
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Browser storage is unavailable");
    expect(listPresets("agent-1")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Deleted preset “Gold”.");
    expect(listPresets("agent-1")).toEqual([]);
    expect(getSelectOption("No presets yet")).toBeTruthy();
  });

  it("an undeclared preset key is listed in form mode and Remove drops it from what is validated", async () => {
    savePreset("agent-1", "Legacy", { tier: "gold", legacy: "x" }, 2);
    validate = (body) => {
      const ctx = body.request_context as Record<string, unknown>;
      if ("legacy" in ctx) return { status: 422, body: { detail: [{ loc: ["body", "request_context", "legacy"], msg: "unknown field", type: "extra" }] } };
      return { status: 200, body: { valid: true, request_context: ctx } };
    };
    await openContext();
    const list = await screen.findByTestId("context-undeclared-keys", {}, SLOW);
    expect(list).toHaveTextContent("legacy");
    await waitFor(() => expect(screen.getByTestId("context-undeclared-keys")).toHaveTextContent("unknown field"), SLOW);
    fireEvent.click(screen.getByRole("button", { name: "Remove legacy" }));
    await waitFor(() => expect(status()).toHaveAttribute("data-state", "valid"), SLOW);
    expect(screen.queryByTestId("context-undeclared-keys")).toBeNull();
    expect(validateBodies.at(-1)).toEqual({ tier: "gold" });
  });

  it("JSON mode keeps an explicit empty string, form mode leaves it out", async () => {
    await openContext();
    await screen.findByLabelText(/Tier/);
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    fireEvent.change(screen.getByLabelText("Request context JSON"), { target: { value: '{"tier":"gold","note":""}' } });
    await waitFor(() => expect(validateBodies.at(-1)).toEqual({ tier: "gold", note: "" }), SLOW);
    fireEvent.click(screen.getByRole("button", { name: "Form" }));
    await waitFor(() => expect(validateBodies.at(-1)).toEqual({ tier: "gold" }), SLOW);
  });
});
