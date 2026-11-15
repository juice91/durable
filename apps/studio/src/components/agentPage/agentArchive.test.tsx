// Follow-up item 7: archive / unarchive an agent (the agent page's More actions menu, the archived
// banner, and the agents list's Active / Archived scope with Unarchive).
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";

const nav = vi.hoisted(() => ({ search: "", replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/agents",
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...original,
    api: { agents: { list: vi.fn(), listArchived: vi.fn(), archive: vi.fn(), unarchive: vi.fn(), get: vi.fn() } },
  };
});
vi.mock("@/components/ui/Toaster", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/components/shortcuts/ShortcutsProvider", () => ({ useCreateAction: () => undefined }));

import { AgentArchiveBlocked, api, ApiError, type Agent } from "@/lib/api";
import { routes } from "@/lib/routes";
import { toast } from "@/components/ui/Toaster";
import { AgentsListUI, scopeFromParams } from "@/components/agents/AgentsList";
import AgentsPage from "@/app/agents/page";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { AgentArchiveMenu, ArchivedBanner, ARCHIVE_DESCRIPTION } from "./AgentArchiveMenu";

// AgentLayout provides the TooltipProvider the header's IconButtons need.
const renderMenu = (ui: React.ReactElement) => render(<TooltipProvider>{ui}</TooltipProvider>);

const agent = (over: Partial<Agent> = {}): Agent =>
  ({ id: "a1", name: "Support", latest_version: 2, archived: false, draft_spec: { name: "Support", model: { provider: "test", name: "m" } }, ...over }) as Agent;

beforeEach(() => {
  vi.clearAllMocks();
  nav.search = "";
  window.localStorage.clear();
});
afterEach(cleanup);

async function openMenu() {
  const trigger = screen.getByTestId("agent-more-actions");
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  fireEvent.keyDown(trigger, { key: "Enter" });
  return screen.findByRole("menu");
}

describe("AgentArchiveMenu", () => {
  it("archives after confirming, and reports the new agent", async () => {
    const onChanged = vi.fn();
    vi.mocked(api.agents.archive).mockResolvedValue(agent({ archived: true }));
    renderMenu(<AgentArchiveMenu agent={agent()} onChanged={onChanged} />);
    const menu = await openMenu();
    fireEvent.click(within(menu).getByText("Archive agent"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(ARCHIVE_DESCRIPTION);
    expect(api.agents.archive).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive agent" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ archived: true })));
    expect(api.agents.archive).toHaveBeenCalledWith("a1", { force: false });
    expect(toast.success).toHaveBeenCalledWith("Archived Support");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("lists the published workflows that run the agent, then archives anyway with force", async () => {
    const onChanged = vi.fn();
    vi.mocked(api.agents.archive)
      .mockRejectedValueOnce(new AgentArchiveBlocked("agent is a step of 1 published workflow(s)", [
        { workflow_id: "w1", workflow_name: "Triage", versions: [1, 2], latest_version: 2 },
      ]))
      .mockResolvedValueOnce(agent({ archived: true }));
    renderMenu(<AgentArchiveMenu agent={agent()} onChanged={onChanged} />);
    fireEvent.click(within(await openMenu()).getByText("Archive agent"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive agent" }));
    const list = await within(dialog).findByTestId("agent-archive-workflows");
    expect(within(list).getByRole("link", { name: "Triage" })).toHaveAttribute("href", routes.workflow("w1"));
    expect(list).toHaveTextContent("versions 1, 2");
    expect(onChanged).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive anyway" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ archived: true })));
    expect(vi.mocked(api.agents.archive).mock.calls).toEqual([["a1", { force: false }], ["a1", { force: true }]]);
  });

  it("keeps the dialog open with the error when archiving fails", async () => {
    const onChanged = vi.fn();
    vi.mocked(api.agents.archive).mockRejectedValue(new ApiError(500, "boom"));
    renderMenu(<AgentArchiveMenu agent={agent()} onChanged={onChanged} />);
    fireEvent.click(within(await openMenu()).getByText("Archive agent"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive agent" }));
    expect(await within(dialog).findByText("Could not archive the agent: boom")).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("an archived agent's menu offers Unarchive, without a confirmation", async () => {
    const onChanged = vi.fn();
    vi.mocked(api.agents.unarchive).mockResolvedValue(agent());
    renderMenu(<AgentArchiveMenu agent={agent({ archived: true })} onChanged={onChanged} />);
    const menu = await openMenu();
    expect(within(menu).queryByText("Archive agent")).toBeNull();
    fireEvent.click(within(menu).getByText("Unarchive agent"));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ archived: false })));
    expect(api.agents.unarchive).toHaveBeenCalledWith("a1");
  });
});

describe("ArchivedBanner", () => {
  it("explains the state, links to the archived list, and unarchives", async () => {
    const onChanged = vi.fn();
    vi.mocked(api.agents.unarchive).mockResolvedValue(agent());
    render(<ArchivedBanner agent={agent({ archived: true })} onChanged={onChanged} />);
    const banner = screen.getByTestId("agent-archived-banner");
    expect(banner).toHaveTextContent("This agent is archived");
    expect(within(banner).getByRole("link", { name: "Archived agents" })).toHaveAttribute("href", "/agents?view=archived");
    fireEvent.click(within(banner).getByRole("button", { name: "Unarchive" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("a failed unarchive is a toast", async () => {
    vi.mocked(api.agents.unarchive).mockRejectedValue(new ApiError(404, "Agent not found"));
    render(<ArchivedBanner agent={agent({ archived: true })} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Unarchive" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("This agent no longer exists."));
  });
});

describe("AgentsListUI archived scope", () => {
  it("shows the scope switch only with onScopeChange, and reports changes", () => {
    const { rerender } = render(<AgentsListUI agents={[]} />);
    expect(screen.queryByRole("radiogroup", { name: "Which agents" })).toBeNull();
    const onScopeChange = vi.fn();
    rerender(<AgentsListUI agents={[]} onScopeChange={onScopeChange} />);
    const archived = screen.getByRole("radio", { name: "Archived" });
    fireEvent.click(archived);
    expect(onScopeChange).toHaveBeenCalledWith("archived");
  });

  it("the archived scope has its own empty state and an Unarchive button per row", () => {
    const onUnarchive = vi.fn();
    const { rerender } = render(<AgentsListUI agents={[]} scope="archived" onScopeChange={vi.fn()} onUnarchive={onUnarchive} />);
    expect(screen.getByText("No archived agents")).toBeInTheDocument();
    rerender(<AgentsListUI agents={[agent()]} scope="archived" onScopeChange={vi.fn()} onUnarchive={onUnarchive} unarchivingId={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Unarchive Support" }));
    expect(onUnarchive).toHaveBeenCalledWith(expect.objectContaining({ id: "a1" }));
    rerender(<AgentsListUI agents={[agent()]} scope="archived" onScopeChange={vi.fn()} onUnarchive={onUnarchive} unarchivingId="a1" />);
    expect(screen.getByRole("button", { name: "Unarchive Support" })).toBeDisabled();
  });

  it("the active scope has no Unarchive button", () => {
    render(<AgentsListUI agents={[agent()]} onScopeChange={vi.fn()} onUnarchive={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Unarchive Support" })).toBeNull();
  });

  it("scopeFromParams reads ?view=archived", () => {
    expect(scopeFromParams(new URLSearchParams("view=archived"))).toBe("archived");
    expect(scopeFromParams(new URLSearchParams("view=x"))).toBe("active");
    expect(scopeFromParams(null)).toBe("active");
  });
});

describe("agents page", () => {
  it("loads the archived agents for ?view=archived, and Unarchive removes the row", async () => {
    nav.search = "view=archived";
    vi.mocked(api.agents.listArchived).mockResolvedValue([agent({ archived: true })]);
    vi.mocked(api.agents.unarchive).mockResolvedValue(agent());
    render(<AgentsPage />);
    expect(await screen.findByText("Support")).toBeInTheDocument();
    expect(api.agents.list).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Unarchive Support" }));
    await waitFor(() => expect(screen.queryByText("Support")).toBeNull());
    expect(api.agents.unarchive).toHaveBeenCalledWith("a1");
  });

  it("switching to Archived writes the scope to the URL", async () => {
    vi.mocked(api.agents.list).mockResolvedValue([agent()]);
    render(<AgentsPage />);
    expect(await screen.findByText("Support")).toBeInTheDocument();
    expect(api.agents.listArchived).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("radio", { name: "Archived" }));
    expect(nav.replace).toHaveBeenCalledWith(routes.agents({ archived: true }));
  });
});
