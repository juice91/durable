// Every page, signed in as a viewer, an editor and an admin: each control is there exactly for the roles that may use it
// (the server's explain answer, `iamFixtures`). The admin sees everything.
import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ path: "/", params: {} as Record<string, string>, search: "" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => nav.params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));
vi.unmock("@/lib/permissions");

import AgentLayoutRoute from "@/app/agents/[id]/layout";
import AgentChatRoute from "@/app/agents/[id]/chat/page";
import AgentEditorRoute from "@/app/agents/[id]/editor/page";
import AgentMemoryRoute from "@/app/agents/[id]/memory/page";
import AgentTestsRoute from "@/app/agents/[id]/tests/page";
import NewAgentPage from "@/app/agents/new/page";
import AgentsPage from "@/app/agents/page";
import ApprovalsPage from "@/app/approvals/page";
import ArchitectRoute from "@/app/architect/page";
import ScorersRoute from "@/app/scorers/page";
import ThreadsRoute from "@/app/threads/page";
import ExternalAccessRoute from "@/app/external-access/page";
import WebhooksRoute from "@/app/webhooks/page";
import WorkflowRunRoute from "@/app/workflows/[id]/runs/[runId]/page";
import WorkflowPage from "@/app/workflows/[id]/page";
import WorkflowSchedulesRoute from "@/app/workflows/[id]/schedules/page";
import WorkflowTestsRoute from "@/app/workflows/[id]/tests/page";
import { FrameHarness } from "@/components/frame/frameTesting";
import { resetCapabilitiesForTests } from "@/lib/iam";
import { iamAnswer, type TestRole } from "./iamFixtures";
import { respond } from "./pageFixtures";
import { json, settle } from "./roleHarness";

const ROLES: TestRole[] = ["viewer", "editor", "admin"];
const RANK: Record<TestRole, number> = { viewer: 1, editor: 2, admin: 3 };

/** The Architect's agent row, as `GET /agents/{id}` answers it. */
const ARCHITECT = { id: "arch", name: "Architect", builtin: true, builtin_role: "architect", latest_version: 1, draft_spec: { spec_version: 1, name: "Architect", instructions: "Design.", model: { provider: "test", name: "echo" }, tools: [] } };
const SCHEDULE = { id: "sch1", workflow_id: "w1", workflow_name: "Triage", name: "Nightly", cron: "0 1 * * *", timezone: "UTC", active: true, input: {}, next_fire_at: null, last_fired_at: null, last_error: null, revision: 1 };

function answer(url: URL): unknown {
  const path = url.pathname.replace(/^\/api\/v1/, "");
  if (path === "/agents/arch") return ARCHITECT;
  if (path === "/agents/arch/versions") return [];
  if (path === "/workflows/w1/schedules") return { items: [SCHEDULE], next_before: null };
  return respond(url);
}

beforeEach(() => {
  window.localStorage.clear();
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1440 });
  vi.stubGlobal("EventSource", class { close() {} addEventListener() {} removeEventListener() {} });
});
afterEach(() => {
  cleanup();
  resetCapabilitiesForTests();
  vi.unstubAllGlobals();
});

const byId = (id: string) => () => screen.queryByTestId(id);
const button = (name: string | RegExp) => () => screen.queryByRole("button", { name });
const link = (name: string | RegExp) => () => screen.queryByRole("link", { name });
const textbox = (name: string | RegExp) => () => screen.queryByPlaceholderText(name);

interface Case {
  page: string;
  path: string;
  params?: Record<string, string>;
  search?: string;
  element: () => React.ReactElement;
  /** Text (or `#testid`) that shows the page has its data. */
  shows: string;
  /** A control and the smallest role that may use it. */
  controls: [string, () => Element | null, TestRole][];
}

const inLayout = (Tab: React.ComponentType) => () => <AgentLayoutRoute><Tab /></AgentLayoutRoute>;

const CASES: Case[] = [
  { page: "agents list", path: "/agents", element: () => <AgentsPage />, shows: "Support bot", controls: [["New agent", link("New agent"), "editor"]] },
  { page: "new agent", path: "/agents/new", element: () => <NewAgentPage />, shows: "New Agent", controls: [["Create agent", button("Create agent"), "editor"]] },
  { page: "agent chat", path: "/agents/a1/chat", params: { id: "a1" }, element: inLayout(AgentChatRoute), shows: "Refund question", controls: [
    ["composer", textbox("Message the agent…"), "editor"],
    ["New conversation", button("New conversation"), "editor"],
    ["rename a conversation", button("Rename Refund question"), "editor"],
    ["archive menu", byId("agent-more-actions"), "admin"],
  ] },
  { page: "Architect agent chat (the exception)", path: "/agents/arch/chat", params: { id: "arch" }, element: inLayout(AgentChatRoute), shows: "#agent-chat", controls: [["composer", textbox("Message the agent…"), "viewer"]] },
  { page: "agent editor", path: "/agents/a1/editor", params: { id: "a1" }, element: inLayout(AgentEditorRoute), shows: "Identity", controls: [
    ["Save draft", button("Save draft"), "editor"],
    ["Publish", button("Publish"), "editor"],
    ["Test draft", byId("editor-test-draft"), "editor"],
  ] },
  { page: "agent memory", path: "/agents/a2/memory", params: { id: "a2" }, search: "resource=alice", element: inLayout(AgentMemoryRoute), shows: "#memory-user-title", controls: [
    ["Forget user", byId("memory-forget-user"), "editor"],
    ["edit the note", byId("memory-working-edit"), "editor"],
  ] },
  { page: "agent tests", path: "/agents/a1/tests", params: { id: "a1" }, element: inLayout(AgentTestsRoute), shows: "Refund suite", controls: [["New suite", byId("eval-suite-new"), "editor"]] },
  { page: "workflow", path: "/workflows/w1", params: { id: "w1" }, element: () => <WorkflowPage />, shows: "classify", controls: [
    ["Run panel", byId("workflow-run-panel"), "editor"],
    ["archive menu", () => screen.queryByRole("button", { name: /More actions|Workflow actions/ }), "admin"],
  ] },
  { page: "workflow schedules", path: "/workflows/w1/schedules", params: { id: "w1" }, element: () => <WorkflowSchedulesRoute />, shows: "Nightly", controls: [
    ["New schedule", button("New schedule"), "editor"],
    ["Pause", button("Pause Nightly"), "editor"],
    ["Delete", button("Delete Nightly"), "admin"],
  ] },
  { page: "workflow tests", path: "/workflows/w1/tests", params: { id: "w1" }, element: () => <WorkflowTestsRoute />, shows: "#eval-suite-row", controls: [["New suite", byId("eval-suite-new"), "editor"]] },
  { page: "paused run", path: "/workflows/w1/runs/wr2", params: { id: "w1", runId: "wr2" }, element: () => <WorkflowRunRoute />, shows: "#workflow-paused-banner", controls: [
    ["Continue", button("Continue"), "admin"],
    ["Stop", button("Stop"), "admin"],
    ["Cancel run", byId("workflow-run-cancel"), "editor"],
  ] },
  { page: "approvals inbox", path: "/approvals", search: "approval=r1:call_1", element: () => <ApprovalsPage />, shows: "#approval-sheet-status", controls: [
    ["Approve", button("Approve"), "admin"],
    ["Deny", button("Deny"), "admin"],
  ] },
  { page: "threads", path: "/threads", element: () => <ThreadsRoute />, shows: "Refund question", controls: [["thread menu", byId("thread-actions"), "editor"]] },
  { page: "webhooks", path: "/webhooks", element: () => <WebhooksRoute />, shows: "#webhook-row", controls: [["Create trigger", byId("webhook-create"), "editor"]] },
  { page: "external access", path: "/external-access", element: () => <ExternalAccessRoute />, shows: "#external-export-row", controls: [
    ["Export a workflow", byId("external-export-create"), "admin"],
    ["New client", byId("external-client-create"), "admin"],
  ] },
  { page: "scorers", path: "/scorers", element: () => <ScorersRoute />, shows: "#reusable-scorers", controls: [["Add scorer", button("Add scorer"), "editor"]] },
  { page: "Architect", path: "/architect", element: () => <ArchitectRoute />, shows: "FAQ bot", controls: [
    ["New", byId("architect-new"), "editor"],
    ["Fill in the form", button("Fill in the form"), "editor"],
    ["Save and go live", button("Save and go live"), "editor"],
    ["model picker", () => screen.queryByRole("combobox", { name: "Change model" }), "admin"],
  ] },
];

describe.each(CASES)("$page", ({ path, params = {}, search = "", element, shows, controls }) => {
  it.each(ROLES)("offers a %s exactly what they may use", async (role) => {
    nav.path = path;
    nav.params = params;
    nav.search = search;
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = new URL(String(input), "http://localhost");
      return iamAnswer(role, url.toString()) ?? json(answer(url));
    }));
    render(<FrameHarness>{element()}</FrameHarness>);
    if (shows.startsWith("#")) expect((await screen.findAllByTestId(shows.slice(1))).length).toBeGreaterThan(0);
    else expect((await screen.findAllByText(shows, { exact: false })).length).toBeGreaterThan(0);
    await settle();
    for (const [name, find, needed] of controls) {
      if (RANK[role] >= RANK[needed]) await vi.waitFor(() => expect(find(), `${name} for a ${role}`).not.toBeNull(), { timeout: 4000 });
      else expect(find(), `${name} for a ${role}`).toBeNull();
    }
  });
});

describe("a webhook trigger", () => {
  it.each(ROLES)("offers a %s the controls its workflow and secret allow", async (role) => {
    nav.path = "/webhooks";
    nav.search = "";
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = new URL(String(input), "http://localhost");
      return iamAnswer(role, url.toString()) ?? json(answer(url));
    }));
    render(<FrameHarness><WebhooksRoute /></FrameHarness>);
    fireEvent.click(await screen.findByRole("button", { name: "github-push" }));
    await screen.findByTestId("webhook-enable-card");
    await settle();
    // Editing the workflow changes and retires a trigger; approving its secret (enable) is an admin's.
    if (role === "viewer") expect(screen.queryByTestId("webhook-archive")).toBeNull();
    else await vi.waitFor(() => expect(screen.queryByTestId("webhook-archive")).not.toBeNull(), { timeout: 4000 });
    if (role === "admin") {
      await vi.waitFor(() => expect(screen.queryByTestId("webhook-enable")).not.toBeNull(), { timeout: 4000 });
      expect(screen.queryByTestId("webhook-rotate-generate")).not.toBeNull();
    } else {
      expect(screen.queryByTestId("webhook-enable")).toBeNull();
      expect(screen.queryByTestId("webhook-rotate-generate")).toBeNull();
      expect(screen.getByTestId("webhook-needs-admin")).toBeTruthy();
    }
  });
});

describe("approvals", () => {
  it.each(ROLES)("lets only an admin decide a tool call (%s)", async (role) => {
    nav.path = "/approvals";
    nav.search = "approval=r1:call_1";
    vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
      const url = new URL(String(input), "http://localhost");
      return iamAnswer(role, url.toString()) ?? json(answer(url));
    }));
    const { ApprovalCard } = await import("@/components/approvals/ApprovalCard");
    render(<ApprovalCard subject={{ kind: "agent", id: "a1" }} approval={{ kind: "tool", runId: "r1", toolCallId: "c", toolName: "send_email", args: {}, deadline: null }} onRefresh={() => undefined} />);
    await settle();
    if (role === "admin") {
      expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
    } else {
      expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
      expect(screen.getByTestId("why-not").textContent).toContain(`Your role (${role}) can't approve runs of this agent.`);
    }
  });
});
