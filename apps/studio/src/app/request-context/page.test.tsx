import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RequestContextPage from "./page";
import { api, type Agent } from "@/lib/api";
import {
  getRequestContext,
  loadSavedContext,
  saveContext,
  validateRequestContext,
  RequestContextValidationError,
  type RequestContextInfo,
} from "@/lib/requestContext";

vi.mock("@/lib/api", () => ({ api: { agents: { list: vi.fn() } } }));
vi.mock("@/lib/requestContext", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/requestContext")>();
  return { ...actual, getRequestContext: vi.fn(), validateRequestContext: vi.fn() };
});
vi.mock("next/link", () => ({
  default: ({ href, children, onClick, ...rest }: { href: string; children: React.ReactNode; onClick?: () => void }) => (
    <a href={href} onClick={(event) => { event.preventDefault(); onClick?.(); }} {...rest}>{children}</a>
  ),
}));

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

describe("RequestContextPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    window.history.replaceState(null, "", "/request-context");
    vi.mocked(api.agents.list).mockResolvedValue(agents);
    vi.mocked(getRequestContext).mockImplementation(async (agentId) => (agentId === "agent-1" ? withFields : noFields));
    vi.mocked(validateRequestContext).mockImplementation(async (_agent, value) => {
      if (!("tier" in value)) throw new RequestContextValidationError(422, null, { tier: "is required" });
      if ("seats" in value && !Number.isInteger(value.seats)) throw new RequestContextValidationError(422, null, { seats: "must be an integer" });
      return value;
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("shows the latest version's fields and schema, and the server's field errors inline", async () => {
    render(<RequestContextPage />);
    expect(await screen.findByRole("table", { name: "Context fields" })).toHaveTextContent("tier");
    expect(getRequestContext).toHaveBeenCalledWith("agent-1", 2);
    expect(screen.getByLabelText("Version")).toHaveValue("2");
    expect(screen.getByTestId("context-json-schema")).toHaveTextContent('"required"');
    expect(await screen.findByTestId("field-error-tier")).toHaveTextContent("is required");
    expect(screen.getByTestId("context-validation-status")).toHaveAttribute("data-state", "invalid");
    expect(screen.getByRole("button", { name: "Save for playground" })).toBeDisabled();
  });

  it("keeps values when switching between form and JSON, validates JSON edits, and saves", async () => {
    render(<RequestContextPage />);
    const tier = await screen.findByLabelText(/Tier/);
    fireEvent.change(tier, { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByTestId("context-validation-status")).toHaveAttribute("data-state", "valid"), { timeout: 2000 });
    expect(validateRequestContext).toHaveBeenLastCalledWith("agent-1", { tier: "gold" }, 2, expect.anything());

    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    const json = screen.getByLabelText("Request context JSON") as HTMLTextAreaElement;
    expect(JSON.parse(json.value)).toEqual({ tier: "gold" });

    fireEvent.change(json, { target: { value: '{"tier": "silver", "seats": "x"}' } });
    expect(await screen.findByTestId("context-json-field-errors", {}, { timeout: 2000 })).toHaveTextContent("seats: must be an integer");

    fireEvent.change(json, { target: { value: "{" } });
    expect(screen.getByTestId("context-json-error")).toHaveTextContent("Enter valid JSON");
    expect(screen.getByRole("button", { name: "Form" })).toBeDisabled();

    fireEvent.change(json, { target: { value: '{"tier": "silver", "seats": 4}' } });
    fireEvent.click(screen.getByRole("button", { name: "Form" }));
    expect(screen.getByLabelText(/Tier/)).toHaveValue("silver");
    expect(screen.getByLabelText(/Seats/)).toHaveValue(4);

    await waitFor(() => expect(screen.getByRole("button", { name: "Save for playground" })).toBeEnabled(), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: "Save for playground" }));
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Saved");
    expect(loadSavedContext("agent-1")).toMatchObject({ value: { tier: "silver", seats: 4 }, version: 2 });
    expect(screen.getByRole("link", { name: "Use in playground" })).toHaveAttribute("href", "/playground?agent=agent-1&version=2&context=preset");
  });

  it("preselects a saved context and its version", async () => {
    saveContext("agent-1", { tier: "silver" }, 1);
    render(<RequestContextPage />);
    expect(await screen.findByLabelText(/Tier/)).toHaveValue("silver");
    expect(screen.getByLabelText("Version")).toHaveValue("1");
    expect(getRequestContext).toHaveBeenCalledWith("agent-1", 1);
    // The link carries the preset's version and the flag that makes the preset win in the playground.
    expect(screen.getByRole("link", { name: "Use in playground" })).toHaveAttribute("href", "/playground?agent=agent-1&version=1&context=preset");
    expect(screen.getByTestId("saved-context")).toHaveTextContent('"silver"');
  });

  it("says so when browser storage is unavailable", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("SecurityError"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("SecurityError"); });
    render(<RequestContextPage />);
    fireEvent.change(await screen.findByLabelText(/Tier/), { target: { value: "gold" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save for playground" })).toBeEnabled(), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: "Save for playground" }));
    expect(screen.getByTestId("context-save-notice")).toHaveTextContent("Browser storage is unavailable");
  });

  it("shows an empty state for an agent without fields and offers only the draft when unpublished", async () => {
    window.history.replaceState(null, "", "/request-context?agent=agent-2");
    render(<RequestContextPage />);
    expect(await screen.findByTestId("context-no-fields")).toHaveTextContent("declares no context fields");
    expect(getRequestContext).toHaveBeenCalledWith("agent-2", "draft");
    const options = Array.from((screen.getByLabelText("Version") as HTMLSelectElement).options).map((option) => option.textContent);
    expect(options).toEqual(["Draft"]);
  });

  it("lists a saved key the version no longer declares in form mode, with Remove", async () => {
    vi.mocked(validateRequestContext).mockImplementation(async (_agent, value) => {
      if ("region" in value) throw new RequestContextValidationError(422, null, { region: "is not a context field of this agent" });
      return value;
    });
    saveContext("agent-1", { tier: "gold", region: "eu" }, 2);
    render(<RequestContextPage />);
    const block = await screen.findByTestId("context-undeclared-keys");
    expect(block).toHaveTextContent("region");
    await waitFor(() => expect(block).toHaveTextContent("is not a context field of this agent"), { timeout: 2000 });
    expect(screen.queryByTestId("field-errors-unmatched")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove region" }));
    expect(screen.queryByTestId("context-undeclared-keys")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("context-validation-status")).toHaveAttribute("data-state", "valid"), { timeout: 2000 });
    expect(validateRequestContext).toHaveBeenLastCalledWith("agent-1", { tier: "gold" }, 2, expect.anything());
  });

  it("shows undeclared-key errors in form mode for a version without fields", async () => {
    vi.mocked(getRequestContext).mockResolvedValue({ ...noFields, agent_id: "agent-1", version: 2, draft: false, fields: [], schema: { type: "object", properties: {} } } as RequestContextInfo);
    vi.mocked(validateRequestContext).mockRejectedValue(new RequestContextValidationError(422, null, { tier: "is not a context field of this agent" }));
    saveContext("agent-1", { tier: "gold" }, 2);
    render(<RequestContextPage />);
    const block = await screen.findByTestId("context-undeclared-keys");
    await waitFor(() => expect(block).toHaveTextContent("tier"), { timeout: 2000 });
  });

  it("sends an explicit empty string from JSON mode but not from form mode", async () => {
    render(<RequestContextPage />);
    await screen.findByLabelText(/Tier/);
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    fireEvent.change(screen.getByLabelText("Request context JSON"), { target: { value: '{"tier": "gold", "note": ""}' } });
    await waitFor(() => expect(validateRequestContext).toHaveBeenLastCalledWith("agent-1", { tier: "gold", note: "" }, 2, expect.anything()), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: "Form" }));
    await waitFor(() => expect(validateRequestContext).toHaveBeenLastCalledWith("agent-1", { tier: "gold" }, 2, expect.anything()), { timeout: 2000 });
  });

  it("notes that saved values are stored unencrypted", async () => {
    render(<RequestContextPage />);
    expect(await screen.findByTestId("saved-context-storage-note")).toHaveTextContent("unencrypted");
  });

  it("switches to the draft", async () => {
    render(<RequestContextPage />);
    await screen.findByRole("table", { name: "Context fields" });
    fireEvent.change(screen.getByLabelText("Version"), { target: { value: "draft" } });
    await waitFor(() => expect(getRequestContext).toHaveBeenCalledWith("agent-1", "draft"));
    expect(window.location.search).toContain("version=draft");
  });
});
