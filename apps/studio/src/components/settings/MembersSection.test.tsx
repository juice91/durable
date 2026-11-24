import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetCapabilitiesForTests } from "@/lib/iam";
import { MembersSection } from "./MembersSection";
import { TooltipProvider } from "@/components/ui/Tooltip";

const fetchMock = vi.fn();
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const members = (mutable = true) => ({
  mutable,
  items: [
    { id: "u1", email: "ada@example.test", display_name: "Ada", roles: ["admin"], status: "active" },
    { id: "u2", email: "bob@example.test", display_name: null, roles: ["viewer"], status: "invited" },
  ],
});
const caps = (transitional: boolean) => ({
  mode: "production", identity: [], policy: { name: "p", transitional, features: [] }, membership: { name: "zitadel", mutable: true },
  me: { subject: "a@b.co", email: "a@b.co", tenant_id: "acme", kind: "user", roles: [], provider: "zitadel" }, actions: ["tenant.manage_members"],
});

function route(handlers: Record<string, () => Response>) {
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${new URL(url, "http://x").pathname}`;
    const handler = handlers[key];
    if (!handler) throw new Error(`unhandled ${key}`);
    return handler();
  });
}

beforeEach(() => {
  resetCapabilitiesForTests();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const view = () => render(<MembersSection />, { wrapper: TooltipProvider });

describe("MembersSection", () => {
  it("lists members with a role picker each, and an invite form", async () => {
    route({ "GET /api/v1/iam/members": () => json(members()), "GET /api/v1/iam/capabilities": () => json(caps(false)) });
    view();
    const rows = await screen.findAllByTestId("member-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByTestId("member-role")).toHaveValue("admin");
    expect(within(rows[1]).getByText("invited")).toBeInTheDocument();
    expect(screen.getByTestId("members-invite")).toBeInTheDocument();
    expect(screen.queryByTestId("members-open-notice")).toBeNull();
  });

  it("changes a role through the API and reloads", async () => {
    let patched: unknown = null;
    route({
      "GET /api/v1/iam/members": () => json(members()),
      "GET /api/v1/iam/capabilities": () => json(caps(false)),
      "PATCH /api/v1/iam/members/u2": () => json({ ...members().items[1], roles: ["editor"] }),
    });
    const original = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") patched = JSON.parse(String(init.body));
      return original(url, init);
    });
    view();
    const rows = await screen.findAllByTestId("member-row");
    fireEvent.change(within(rows[1]).getByTestId("member-role"), { target: { value: "editor" } });
    await waitFor(() => expect(patched).toEqual({ role: "editor" }));
  });

  it("shows the API's refusal when the last admin is demoted", async () => {
    route({
      "GET /api/v1/iam/members": () => json(members()),
      "GET /api/v1/iam/capabilities": () => json(caps(false)),
      "PATCH /api/v1/iam/members/u1": () => json({ detail: "the last admin cannot be demoted; promote someone else first", code: "last_admin" }, 409),
    });
    view();
    const rows = await screen.findAllByTestId("member-row");
    fireEvent.change(within(rows[0]).getByTestId("member-role"), { target: { value: "viewer" } });
    expect(await screen.findByTestId("members-error")).toHaveTextContent("last admin cannot be demoted");
  });

  it("invites by e-mail and role", async () => {
    let body: unknown = null;
    route({
      "GET /api/v1/iam/members": () => json(members()),
      "GET /api/v1/iam/capabilities": () => json(caps(false)),
      "POST /api/v1/iam/members": () => json({ id: "u3", email: "cy@example.test", display_name: null, roles: ["editor"], status: "invited" }, 201),
    });
    const original = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") body = JSON.parse(String(init.body));
      return original(url, init);
    });
    view();
    await screen.findAllByTestId("member-row");
    fireEvent.change(screen.getByLabelText("E-mail"), { target: { value: "cy@example.test" } });
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "editor" } });
    fireEvent.click(screen.getByTestId("members-invite-submit"));
    await waitFor(() => expect(body).toEqual({ email: "cy@example.test", role: "editor" }));
  });

  it("removes a member after a confirmation", async () => {
    let deleted = false;
    route({
      "GET /api/v1/iam/members": () => json(members()),
      "GET /api/v1/iam/capabilities": () => json(caps(false)),
      "DELETE /api/v1/iam/members/u2": () => { deleted = true; return new Response(null, { status: 204 }); },
    });
    view();
    const rows = await screen.findAllByTestId("member-row");
    fireEvent.click(within(rows[1]).getByTestId("member-remove"));
    expect(deleted).toBe(false);
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    await waitFor(() => expect(deleted).toBe(true));
  });

  it("is read only when the source is not mutable: no controls, 'managed in' note", async () => {
    route({ "GET /api/v1/iam/members": () => json(members(false)), "GET /api/v1/iam/capabilities": () => json(caps(false)) });
    view();
    await screen.findAllByTestId("member-row");
    expect(screen.getByTestId("members-readonly")).toHaveTextContent("managed in");
    expect(screen.queryByTestId("members-invite")).toBeNull();
    expect(screen.queryByTestId("member-role")).toBeNull();
    expect(screen.getAllByTestId("member-role-text")[0]).toHaveTextContent("admin");
  });

  it("under the transitional policy engine it says access is open", async () => {
    route({ "GET /api/v1/iam/members": () => json(members()), "GET /api/v1/iam/capabilities": () => json(caps(true)) });
    view();
    expect(await screen.findByTestId("members-open-notice")).toHaveTextContent("every signed-in user acts as an admin");
  });

  it("a 404 (no membership source) says it is not available", async () => {
    route({ "GET /api/v1/iam/members": () => json({ detail: "not found" }, 404), "GET /api/v1/iam/capabilities": () => json(caps(false)) });
    view();
    expect(await screen.findByTestId("members-error")).toHaveTextContent("not available");
  });
});
