import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AccessView, Explanation } from "@/lib/access";
import type { IamCapabilities } from "@/lib/iam";
import { reportRefusal } from "@/lib/refusals";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import { chooseOption } from "@/test/select";

const mocks = vi.hoisted(() => ({
  getResourceAccess: vi.fn(), addAccessGrant: vi.fn(), removeAccessGrant: vi.fn(), explainAccess: vi.fn(), listMembers: vi.fn(), loadCapabilities: vi.fn(),
}));
vi.mock("@/lib/access", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getResourceAccess: mocks.getResourceAccess, addAccessGrant: mocks.addAccessGrant, removeAccessGrant: mocks.removeAccessGrant, explainAccess: mocks.explainAccess,
}));
vi.mock("@/lib/members", async (importOriginal) => ({ ...(await importOriginal<object>()), listMembers: mocks.listMembers }));
vi.mock("@/lib/iam", async (importOriginal) => ({ ...(await importOriginal<object>()), loadCapabilities: mocks.loadCapabilities }));

import { PermissionNotice } from "./PermissionNotice";
import { RefusalNotice } from "./RefusalNotice";
import { ResourceAccessPanel } from "./ResourceAccessPanel";

const caps = (features: string[]): IamCapabilities => ({
  mode: "production", identity: [], membership: { name: "zitadel", mutable: true }, actions: [],
  policy: { name: features.length ? "openfga" : "openaccess", transitional: features.length === 0, features },
  me: { subject: "zitadel:me", email: null, tenant_id: "t", kind: "user", roles: ["viewer"], provider: "zitadel" },
});
const FULL = caps(["grants", "explain", "list_filter"]);

const view = (over: Partial<AccessView> = {}): AccessView => ({
  resource: { kind: "workflow", id: "w1" }, supported: true, can_manage: false, used_by: [],
  relations: [
    { relation: "owner", roles: [] }, { relation: "editor", roles: ["admin", "editor", "viewer"] }, { relation: "runner", roles: ["admin", "editor", "viewer"] },
  ],
  grants: [
    { relation: "owner", subject_type: "user", subject: "ann@example.test" },
    { relation: "runner", subject_type: "role", subject: "viewer" },
  ],
  ...over,
});

const explanation = (over: Partial<Explanation> = {}): Explanation => ({
  supported: true, kind: "workflow", id: "w1", roles: ["viewer"],
  decisions: [
    { action: "run", allowed: true, reason: "allowed", needed: null },
    { action: "publish", allowed: false, reason: "no_permission", needed: { roles: ["admin", "editor"], relations: ["owner", "editor"] } },
  ],
  ...over,
});

beforeEach(() => {
  Object.values(mocks).forEach((m) => m.mockReset());
  mocks.loadCapabilities.mockResolvedValue(FULL);
  mocks.getResourceAccess.mockResolvedValue(view());
  mocks.addAccessGrant.mockResolvedValue(undefined);
  mocks.removeAccessGrant.mockResolvedValue(undefined);
  mocks.explainAccess.mockResolvedValue(explanation());
  mocks.listMembers.mockResolvedValue({ mutable: true, items: [{ id: "u2", email: "bo@example.test", display_name: "Bo", roles: ["viewer"], status: "active" }] });
});
afterEach(() => cleanup());

describe("ResourceAccessPanel", () => {
  it.each(["agent", "workflow", "knowledge_base", "secret", "mcp_server"] as const)("asks about the %s it is on and lists who holds which relation", async (kind) => {
    render(<ResourceAccessPanel kind={kind} id="r1" />);
    const owner = await screen.findByTestId("access-person-owner-user-ann@example.test");
    expect(owner).toHaveTextContent("ann@example.test");
    expect(owner).toHaveTextContent("Owner");
    expect(screen.getByTestId("access-person-runner-role-viewer")).toHaveTextContent("Everyone with the viewer role or above");
    expect(mocks.getResourceAccess).toHaveBeenCalledWith(kind, "r1", expect.anything());
  });

  it("is read-only for someone who is not an admin, says so, and never lists members", async () => {
    render(<ResourceAccessPanel kind="workflow" id="w1" />);
    await screen.findByTestId("access-person-owner-user-ann@example.test");
    expect(screen.getByTestId("access-read-only")).toHaveTextContent("Only an admin can change access.");
    expect(screen.queryByRole("button", { name: /Remove/ })).toBeNull();
    expect(screen.queryByTestId("access-add")).toBeNull();
    expect(mocks.listMembers).not.toHaveBeenCalled();
  });

  it("lets an admin add a member or a role with one of the model's relations", async () => {
    mocks.getResourceAccess.mockResolvedValue(view({ can_manage: true }));
    render(<ResourceAccessPanel kind="workflow" id="w1" />);
    await screen.findByTestId("access-add");
    await waitFor(() => expect(mocks.listMembers).toHaveBeenCalled());
    await chooseOption(screen.getByRole("combobox", { name: "Who" }), "bo@example.test");
    await chooseOption(screen.getByRole("combobox", { name: "Relation" }), "Runner");
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mocks.addAccessGrant).toHaveBeenCalledWith("workflow", "w1", { relation: "runner", member_id: "u2" }));
    await waitFor(() => expect(mocks.getResourceAccess).toHaveBeenCalledTimes(2));
    await chooseOption(screen.getByRole("combobox", { name: "Who" }), "Role: editor");
    await chooseOption(screen.getByRole("combobox", { name: "Relation" }), "Editor");
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mocks.addAccessGrant).toHaveBeenLastCalledWith("workflow", "w1", { relation: "editor", role: "editor" }));
  });

  it("offers a role only the relations a role may hold (an owner is a person)", async () => {
    mocks.getResourceAccess.mockResolvedValue(view({ can_manage: true }));
    render(<ResourceAccessPanel kind="workflow" id="w1" />);
    await screen.findByTestId("access-add");
    await chooseOption(screen.getByRole("combobox", { name: "Who" }), "Role: viewer");
    fireEvent.click(screen.getByRole("combobox", { name: "Relation" }));
    const listbox = await screen.findByRole("listbox");
    expect(within(listbox).queryByRole("option", { name: "Owner" })).toBeNull();
    expect(within(listbox).getByRole("option", { name: "Runner" })).toBeInTheDocument();
  });

  it("lets an admin remove a grant, and shows a refusal of the change", async () => {
    mocks.getResourceAccess.mockResolvedValue(view({ can_manage: true }));
    mocks.removeAccessGrant.mockRejectedValue(new Error("boom"));
    render(<ResourceAccessPanel kind="workflow" id="w1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove Runner for viewer" }));
    await waitFor(() => expect(mocks.removeAccessGrant).toHaveBeenCalledWith("workflow", "w1", { relation: "runner", subject_type: "role", subject: "viewer" }));
    expect(await screen.findByTestId("resource-access-error")).toHaveTextContent("The change was not saved.");
  });

  it("has no accessibility violations with grants, the add form and the users of a secret", async () => {
    mocks.getResourceAccess.mockResolvedValue(view({ can_manage: true, used_by: [{ kind: "agent", id: "a1" }] }));
    const { container } = render(<ResourceAccessPanel kind="secret" id="crm" />);
    await screen.findByTestId("access-add");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);

  it("links the agents and workflows that may use a secret", async () => {
    mocks.getResourceAccess.mockResolvedValue(view({ resource: { kind: "secret", id: "crm" }, used_by: [{ kind: "agent", id: "a1" }, { kind: "workflow", id: "w9" }] }));
    render(<ResourceAccessPanel kind="secret" id="crm" />);
    const used = await screen.findByTestId("access-used-by");
    expect(within(used).getByRole("link", { name: /a1/ })).toHaveAttribute("href", "/agents/a1/access");
    expect(within(used).getByRole("link", { name: /w9/ })).toHaveAttribute("href", "/workflows/w9?tab=access");
  });

  it("says fine-grained access is off under openaccess, without asking for grants", async () => {
    mocks.loadCapabilities.mockResolvedValue(caps([]));
    render(<ResourceAccessPanel kind="agent" id="a1" />);
    expect(await screen.findByTestId("access-off")).toHaveTextContent("Fine-grained access is off");
    expect(mocks.getResourceAccess).not.toHaveBeenCalled();
  });
});

describe("PermissionNotice (why can't I?)", () => {
  it("names the owner and says why each refused action is refused and what to ask for; allowed ones are not listed", async () => {
    render(<PermissionNotice kind="workflow" id="w1" actions={["run", "publish"]} />);
    const notice = await screen.findByTestId("why-not");
    expect(notice).toHaveTextContent("Your role (viewer) can't publish this workflow. Ask an admin for the editor role or an editor grant on it.");
    expect(notice).not.toHaveTextContent("can't run");
    expect(await screen.findByTestId("resource-owner")).toHaveTextContent("Owner: ann@example.test");
    expect(mocks.explainAccess).toHaveBeenCalledWith("workflow", "w1", ["run", "publish"], expect.anything());
  });

  it("shows only the owner when everything is allowed, and nothing at all under openaccess", async () => {
    mocks.explainAccess.mockResolvedValue(explanation({ decisions: [{ action: "run", allowed: true, reason: "allowed", needed: null }] }));
    const { unmount } = render(<PermissionNotice kind="workflow" id="w1" actions={["run"]} />);
    await screen.findByTestId("resource-owner");
    expect(screen.queryByTestId("why-not")).toBeNull();
    unmount();
    mocks.loadCapabilities.mockResolvedValue(caps([]));
    mocks.explainAccess.mockClear();
    mocks.getResourceAccess.mockClear();
    const { container } = render(<PermissionNotice kind="workflow" id="w1" actions={["run"]} />);
    await act(async () => { await Promise.resolve(); });
    expect(container).toBeEmptyDOMElement();
    expect(mocks.explainAccess).not.toHaveBeenCalled();
    expect(mocks.getResourceAccess).not.toHaveBeenCalled();
  });
});

describe("RefusalNotice", () => {
  it("explains the last refused call and can be dismissed", async () => {
    render(<RefusalNotice />);
    await waitFor(() => expect(mocks.loadCapabilities).toHaveBeenCalled());
    act(() => reportRefusal({ type: "refused", action: "publish", kind: "workflow", id: "w1" }));
    expect(await screen.findByTestId("refusal-notice")).toHaveTextContent("Your role (viewer) can't publish this workflow.");
    expect(mocks.explainAccess).toHaveBeenCalledWith("workflow", "w1", ["publish"], expect.anything());
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByTestId("refusal-notice")).toBeNull();
  });

  it("says the engine is unavailable", async () => {
    render(<RefusalNotice />);
    await waitFor(() => expect(mocks.loadCapabilities).toHaveBeenCalled());
    act(() => reportRefusal({ type: "unavailable" }));
    expect(await screen.findByTestId("refusal-notice")).toHaveTextContent("the authorization engine is unavailable");
    expect(mocks.explainAccess).not.toHaveBeenCalled();
  });

  it("stays silent under openaccess", async () => {
    mocks.loadCapabilities.mockResolvedValue(caps([]));
    render(<RefusalNotice />);
    await waitFor(() => expect(mocks.loadCapabilities).toHaveBeenCalled());
    act(() => reportRefusal({ type: "refused", action: "publish", kind: "workflow", id: "w1" }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId("refusal-notice")).toBeNull();
  });
});
