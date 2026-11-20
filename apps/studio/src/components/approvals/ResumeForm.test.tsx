import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ApprovalCard } from "./ApprovalCard";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { validateAgainstSchema, serverFieldErrors } from "@/lib/schemaValidation";
import type { JsonSchema } from "@/lib/tools";

const SCHEMA: JsonSchema = {
  type: "object",
  properties: {
    email: { type: "string", title: "Email" },
    count: { type: "integer", minimum: 1, maximum: 9 },
    tier: { type: "string", enum: ["free", "pro"] },
    address: { type: "object", properties: { city: { type: "string" } }, required: ["city"] },
  },
  required: ["email", "count"],
};

const fetchMock = vi.fn();
beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function card(onRefresh = vi.fn()) {
  render(
    <ApprovalCard
      approval={{ kind: "step", runId: "r1", stepId: "ask", prompt: "Details?", deadline: null, resumeSchema: SCHEMA }}
      onRefresh={onRefresh}
    />,
  );
  return onRefresh;
}

describe("validateAgainstSchema", () => {
  it("reports required, type, enum, bounds and nested required without echoing values", () => {
    const errors = validateAgainstSchema(SCHEMA, { count: 0, tier: "secret-x", address: {} });
    expect(errors.email).toBe("Email is required");
    expect(errors.count).toBe("Count must be at least 1");
    expect(errors.tier).toBe("Tier must be one of the listed options");
    expect(errors["address.city"]).toBe("City is required");
    expect(JSON.stringify(errors)).not.toContain("secret-x");
    expect(validateAgainstSchema(SCHEMA, { email: "a", count: 2, address: { city: "Oslo" } })).toEqual({});
    expect(validateAgainstSchema(SCHEMA, { email: "a", count: 1.5 }).count).toBe("Count must be an integer");
  });

  it("maps a 422's body.data locations", () => {
    expect(serverFieldErrors([{ loc: ["body", "data", "address", "city"], msg: "bad" }, { loc: ["body", "x"], msg: "no" }]))
      .toEqual({ "address.city": "bad" });
  });
});

describe("resume data form on a suspended input step", () => {
  it("renders the generated fields, Submit and Reject (no Approve), and has no axe violations", async () => {
    const { container } = render(
      <ApprovalCard approval={{ kind: "step", runId: "r1", stepId: "ask", prompt: "Details?", deadline: null, resumeSchema: SCHEMA }} onRefresh={vi.fn()} />,
    );
    expect(screen.getByRole("heading", { name: /Waiting for input/ })).toBeTruthy();
    expect(screen.getByLabelText(/Email/)).toBeTruthy();
    expect(screen.getByLabelText(/Tier/)).toHaveAttribute("data-select-trigger");
    expect(screen.getByRole("group", { name: /Address/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Submit" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);

  it("blocks an invalid submit client-side with field messages and sends nothing", async () => {
    card();
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(await screen.findByTestId("field-error-email")).toBeTruthy();
    expect(screen.getByTestId("field-error-count").textContent).toBe("Count is required");
  });

  it("submits the typed object to the resume endpoint", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ decision: "approved" }), { status: 200 }));
    const onRefresh = card();
    fireEvent.change(screen.getByLabelText(/Email/), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByLabelText(/Count/), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText(/Tier/), { target: { value: "pro" } });
    fireEvent.change(screen.getByLabelText(/City/), { target: { value: "Oslo" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/steps/ask/resume"));
    expect(calls).toHaveLength(1);
    expect(JSON.parse(calls[0][1].body)).toEqual({ data: { email: "a@example.com", count: 3, tier: "pro", address: { city: "Oslo" } } });
  });

  it("shows the server's 422 field errors and keeps the form", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: [{ loc: ["body", "data", "email"], msg: "does not satisfy the step's schema ('format' keyword)", type: "json_schema.format" }] }), { status: 422 }),
    );
    card();
    fireEvent.change(screen.getByLabelText(/Email/), { target: { value: "nope" } });
    fireEvent.change(screen.getByLabelText(/Count/), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText(/City/), { target: { value: "Oslo" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect((await screen.findByTestId("field-error-email")).textContent).toContain("format");
    expect((screen.getByLabelText(/Email/) as HTMLInputElement).value).toBe("nope");
  });

  it("explains a 409 (already decided)", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "step is not waiting for approval" }), { status: 409 }));
    card();
    fireEvent.change(screen.getByLabelText(/Email/), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByLabelText(/Count/), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText(/City/), { target: { value: "Oslo" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/no longer waiting/);
  });
});
