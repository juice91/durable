import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RequestContextPage from "./page";
import { credentialScope, loadSavedContext, saveContext, savedContextKey } from "@/lib/requestContext";
import { routes } from "@/lib/routes";

vi.mock("next/link", () => ({
  default: ({ href, children, onClick, ...rest }: { href: string; children: React.ReactNode; onClick?: () => void }) => (
    <a href={href} onClick={(event) => { event.preventDefault(); onClick?.(); }} {...rest}>{children}</a>
  ),
}));

const spec = { spec_version: 1, name: "Support", instructions: "", model: { provider: "test", name: "echo" }, tools: [] };
const AGENTS = [{ id: "agent-1", name: "Support", draft_spec: spec, latest_version: 2 }];
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
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  window.localStorage.clear();
  window.history.replaceState(null, "", routes.requestContext());
  validateBodies = [];
  validate = (body) => ({ status: 200, body: { valid: true, request_context: body.request_context } });
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input);
    let reply: Reply = { status: 404, body: { detail: "nf" } };
    if (url.pathname === "/api/v1/agents") reply = { status: 200, body: AGENTS };
    else if (url.pathname.endsWith("/request-context/validate")) {
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

describe("Request context page acceptance", () => {
  it("shows each field's own server error next to its input", async () => {
    validate = () => ({
      status: 422,
      body: { detail: [
        { loc: ["body", "request_context", "seats"], msg: "must be an integer", type: "int_parsing" },
        { loc: ["body", "request_context", "note"], msg: "is too long", type: "string_too_long" },
        { loc: ["body", "request_context", "tier"], msg: "is required", type: "missing" },
      ] },
    });
    render(<RequestContextPage />);
    expect(await screen.findByTestId("field-error-seats", {}, SLOW)).toHaveTextContent("must be an integer");
    expect(screen.getByTestId("field-error-note")).toHaveTextContent("is too long");
    expect(screen.getByTestId("field-error-tier")).toHaveTextContent("is required");
    expect(screen.getByRole("button", { name: "Save for playground" })).toBeDisabled();
  });

  it("live validation drops a stale response that arrives after a newer one", async () => {
    let releaseFirst: (r: Reply) => void = () => {};
    let n = 0;
    validate = (body) => {
      n += 1;
      if (n === 1) return new Promise<Reply>((r) => { releaseFirst = r; });
      return { status: 200, body: { valid: true, request_context: body.request_context } };
    };
    render(<RequestContextPage />);
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
    expect(screen.getByRole("button", { name: "Save for playground" })).toBeEnabled();
  });

  it("a transport failure is an error state that cannot be saved", async () => {
    validate = () => ({ status: 500, body: { detail: "database down" } });
    render(<RequestContextPage />);
    await screen.findByLabelText(/Tier/);
    await waitFor(() => expect(status()).toHaveAttribute("data-state", "error"), SLOW);
    expect(screen.getByTestId("context-validation-message")).toHaveTextContent("database down");
    expect(screen.getByRole("button", { name: "Save for playground" })).toBeDisabled();
  });

  it("saving stores the accepted context under a credential-scoped key, not the token", async () => {
    render(<RequestContextPage />);
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save for playground" })).toBeEnabled(), SLOW);
    fireEvent.click(screen.getByRole("button", { name: "Save for playground" }));
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Saved");
    const keys = Object.keys(window.localStorage);
    expect(keys).toEqual([`eap.requestContext.v1:${credentialScope()}:agent-1`]);
    expect(keys[0]).toMatch(/^eap\.requestContext\.v1:c[0-9a-f]{16}:agent-1$/);
    expect(keys[0]).not.toContain("test");
    expect(loadSavedContext("agent-1")).toMatchObject({ value: { tier: "gold" }, version: 2 });
    expect(screen.getByTestId("saved-context-storage-note")).toHaveTextContent("unencrypted");
  });

  it("another credential never sees the saved context", async () => {
    saveContext("agent-1", { tier: "silver" }, 2);
    expect(window.localStorage.getItem(savedContextKey("agent-1"))).not.toBeNull();
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer someone-else");
    expect(loadSavedContext("agent-1")).toBeNull();
    render(<RequestContextPage />);
    expect(await screen.findByLabelText(/Tier/)).toHaveValue("");
    expect(screen.getByTestId("saved-context")).toHaveTextContent("Nothing saved");
  });

  it("a storage that throws on save is reported, and nothing is claimed saved", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    render(<RequestContextPage />);
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save for playground" })).toBeEnabled(), SLOW);
    fireEvent.click(screen.getByRole("button", { name: "Save for playground" }));
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Browser storage is unavailable");
    expect(screen.getByTestId("saved-context")).toHaveTextContent("Nothing saved");
  });

  it("a storage that throws on read still renders the form", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    render(<RequestContextPage />);
    expect(await screen.findByLabelText(/Tier/)).toHaveValue("");
  });

  it("Remove saved context clears the entry; a storage failure on removal is reported", async () => {
    saveContext("agent-1", { tier: "gold" }, 2);
    render(<RequestContextPage />);
    await screen.findByLabelText(/Tier/);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementationOnce(() => {
      throw new Error("SecurityError");
    });
    fireEvent.click(screen.getByRole("button", { name: "Remove saved context" }));
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Browser storage is unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Remove saved context" }));
    expect(screen.getByTestId("saved-context")).toHaveTextContent("Nothing saved");
    expect(window.localStorage.getItem(savedContextKey("agent-1"))).toBeNull();
  });

  it("an undeclared saved key is listed in form mode and Remove drops it from what is validated", async () => {
    saveContext("agent-1", { tier: "gold", legacy: "x" }, 2);
    validate = (body) => {
      const ctx = body.request_context as Record<string, unknown>;
      if ("legacy" in ctx) return { status: 422, body: { detail: [{ loc: ["body", "request_context", "legacy"], msg: "unknown field", type: "extra" }] } };
      return { status: 200, body: { valid: true, request_context: ctx } };
    };
    render(<RequestContextPage />);
    const list = await screen.findByTestId("context-undeclared-keys", {}, SLOW);
    expect(list).toHaveTextContent("legacy");
    await waitFor(() => expect(screen.getByTestId("context-undeclared-keys")).toHaveTextContent("unknown field"), SLOW);
    fireEvent.click(screen.getByRole("button", { name: "Remove legacy" }));
    await waitFor(() => expect(status()).toHaveAttribute("data-state", "valid"), SLOW);
    expect(screen.queryByTestId("context-undeclared-keys")).toBeNull();
    expect(validateBodies.at(-1)).toEqual({ tier: "gold" });
  });

  it("JSON mode keeps an explicit empty string, form mode leaves it out", async () => {
    render(<RequestContextPage />);
    await screen.findByLabelText(/Tier/);
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    fireEvent.change(screen.getByLabelText("Request context JSON"), { target: { value: '{"tier":"gold","note":""}' } });
    await waitFor(() => expect(validateBodies.at(-1)).toEqual({ tier: "gold", note: "" }), SLOW);
    fireEvent.click(screen.getByRole("button", { name: "Form" }));
    await waitFor(() => expect(validateBodies.at(-1)).toEqual({ tier: "gold" }), SLOW);
  });
});
