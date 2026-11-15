// Follow-up 7 acceptance (Studio): archive and unarchive through the real agent page layout, the
// agents list's Active / Archived switch with its URL state, error paths and axe on the new UI.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";

const nav = vi.hoisted(() => ({ path: "/agents/a1/chat", search: "", params: { id: "a1" } as Record<string, string>, push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => nav.params,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
  redirect: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...original,
    api: {
      agents: { get: vi.fn(), list: vi.fn(), listArchived: vi.fn(), archive: vi.fn(), unarchive: vi.fn() },
      versions: { list: vi.fn() },
    },
  };
});
vi.mock("@/components/ui/Toaster", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/ui/Toaster")>()),
  toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/components/shortcuts/ShortcutsProvider", () => ({ useCreateAction: () => undefined }));

import { api, ApiError, type Agent } from "@/lib/api";
import { routes } from "@/lib/routes";
import { toast } from "@/components/ui/Toaster";
import { AgentLayout } from "@/components/agentPage/AgentLayout";
import AgentsPage from "@/app/agents/page";
import { FrameHarness, mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH, type MatchMediaControl } from "@/components/frame/frameTesting";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "./a11y";

const spec = { spec_version: 1, name: "Support bot", instructions: "Help", model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [] };
const agent = (over: Partial<Agent> = {}): Agent => ({ id: "a1", name: "Support bot", draft_spec: spec, latest_version: 1, archived: false, ...over }) as Agent;

let media: MatchMediaControl | undefined;
let restoreWidth: () => void = () => {};
beforeEach(() => {
  vi.clearAllMocks();
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  window.localStorage.clear();
  nav.path = "/agents/a1/chat";
  nav.search = "";
  nav.params = { id: "a1" };
  vi.mocked(api.agents.get).mockResolvedValue(agent());
  vi.mocked(api.agents.list).mockResolvedValue([agent()]);
  vi.mocked(api.agents.listArchived).mockResolvedValue([]);
  vi.mocked(api.versions.list).mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  media?.restore();
  restoreWidth();
  document.documentElement.removeAttribute("data-sidebar");
});

function renderLayout() {
  media = mockMatchMedia(true);
  return render(
    <FrameHarness>
      <AgentLayout agentId="a1">
        <p>page body</p>
      </AgentLayout>
    </FrameHarness>,
  );
}
async function openMenu() {
  const trigger = await screen.findByTestId("agent-more-actions");
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  fireEvent.keyDown(trigger, { key: "Enter" });
  return screen.findByRole("menu");
}

describe("archive flow on the agent page", () => {
  it("Cancel archives nothing; Archive shows the banner and the menu offers Unarchive; the banner unarchives", async () => {
    renderLayout();
    fireEvent.click(within(await openMenu()).getByText("Archive agent"));
    let dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Archive Support bot?");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.agents.archive).not.toHaveBeenCalled();
    expect(screen.queryByTestId("agent-archived-banner")).toBeNull();

    vi.mocked(api.agents.archive).mockResolvedValue(agent({ archived: true }));
    fireEvent.click(within(await openMenu()).getByText("Archive agent"));
    dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive agent" }));
    const banner = await screen.findByTestId("agent-archived-banner");
    expect(api.agents.archive).toHaveBeenCalledTimes(1);
    expect(api.agents.archive).toHaveBeenCalledWith("a1", { force: false });
    expect(toast.success).toHaveBeenCalledWith("Archived Support bot");
    expect(banner).toHaveTextContent("new chats, runs and publishes are refused");
    expect(screen.getByText("page body")).toBeInTheDocument(); // the page stays readable
    const menu = await openMenu();
    expect(within(menu).queryByText("Archive agent")).toBeNull();
    fireEvent.keyDown(menu, { key: "Escape" });

    vi.mocked(api.agents.unarchive).mockResolvedValue(agent());
    fireEvent.click(within(banner).getByRole("button", { name: "Unarchive" }));
    await waitFor(() => expect(screen.queryByTestId("agent-archived-banner")).toBeNull());
    expect(api.agents.unarchive).toHaveBeenCalledWith("a1");
    expect(toast.success).toHaveBeenCalledWith("Unarchived Support bot");
  });

  it("an agent that is archived on load shows the banner at once and no Archive item", async () => {
    vi.mocked(api.agents.get).mockResolvedValue(agent({ archived: true }));
    renderLayout();
    const banner = await screen.findByTestId("agent-archived-banner");
    expect(within(banner).getByRole("link", { name: "Archived agents" })).toHaveAttribute("href", routes.agents({ archived: true }));
    const menu = await openMenu();
    expect(within(menu).getByText("Unarchive agent")).toBeInTheDocument();
    expect(within(menu).queryByText("Archive agent")).toBeNull();
  });

  it("an archive that finds the agent gone says so in the dialog and keeps the page unarchived", async () => {
    vi.mocked(api.agents.archive).mockRejectedValue(new ApiError(404, "Agent not found"));
    renderLayout();
    fireEvent.click(within(await openMenu()).getByText("Archive agent"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive agent" }));
    expect(await within(dialog).findByText("This agent no longer exists.")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-archived-banner")).toBeNull();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("has no axe violations with the dialog open or the banner shown", async () => {
    vi.mocked(api.agents.get).mockResolvedValue(agent({ archived: true }));
    renderLayout();
    await screen.findByTestId("agent-archived-banner");
    await expectNoAxeViolations(document.body);
    cleanup();
    vi.mocked(api.agents.get).mockResolvedValue(agent());
    renderLayout();
    fireEvent.click(within(await openMenu()).getByText("Archive agent"));
    await screen.findByRole("dialog");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("agents list: Active / Archived switch", () => {
  const archived = [agent({ id: "x1", name: "Old bot", archived: true }), agent({ id: "x2", name: "Older bot", archived: true })];

  it("the Active view asks for the active list only; the switch writes ?view=archived and back writes /agents", async () => {
    render(<AgentsPage />);
    expect(await screen.findByText("Support bot")).toBeInTheDocument();
    expect(api.agents.list).toHaveBeenCalledTimes(1);
    expect(api.agents.listArchived).not.toHaveBeenCalled();
    expect(screen.getByRole("radio", { name: "Active" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "Archived" }));
    expect(nav.replace).toHaveBeenLastCalledWith("/agents?view=archived");
    expect(routes.agents({ archived: true })).toBe("/agents?view=archived");
    expect(routes.agents()).toBe("/agents");
  });

  it("?view=archived lists archived agents with Unarchive per row, and a row leaves the list once unarchived", async () => {
    nav.path = "/agents";
    nav.search = "view=archived";
    vi.mocked(api.agents.listArchived).mockResolvedValue(archived);
    vi.mocked(api.agents.unarchive).mockResolvedValue(agent({ id: "x1", name: "Old bot" }));
    render(<AgentsPage />);
    expect((await screen.findAllByText("Old bot")).length).toBeGreaterThan(0);
    expect(api.agents.list).not.toHaveBeenCalled();
    expect(screen.getByRole("radio", { name: "Archived" })).toBeChecked();
    fireEvent.click(screen.getAllByRole("button", { name: "Unarchive Old bot" })[0]);
    await waitFor(() => expect(screen.queryAllByText("Old bot")).toHaveLength(0));
    expect(api.agents.unarchive).toHaveBeenCalledTimes(1);
    expect(api.agents.unarchive).toHaveBeenCalledWith("x1");
    expect(toast.success).toHaveBeenCalledWith("Unarchived Old bot");
    expect(screen.getAllByText("Older bot").length).toBeGreaterThan(0);
  });

  it("a failed unarchive keeps the row and toasts; an empty archive has its own empty state", async () => {
    nav.search = "view=archived";
    vi.mocked(api.agents.listArchived).mockResolvedValue([archived[0]]);
    vi.mocked(api.agents.unarchive).mockRejectedValue(new ApiError(500, "db down"));
    render(<AgentsPage />);
    fireEvent.click((await screen.findAllByRole("button", { name: "Unarchive Old bot" }))[0]);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Could not unarchive the agent: db down"));
    expect(screen.getAllByText("Old bot").length).toBeGreaterThan(0);
    cleanup();
    vi.mocked(api.agents.listArchived).mockResolvedValue([]);
    render(<AgentsPage />);
    expect(await screen.findByText("No archived agents")).toBeInTheDocument();
  });

  it("a failed archived load says so, and axe finds nothing on the archived list with its switch", async () => {
    nav.search = "view=archived";
    vi.mocked(api.agents.listArchived).mockRejectedValueOnce(new ApiError(500, "x"));
    render(<AgentsPage />);
    expect(await screen.findByText("Failed to load archived agents")).toBeInTheDocument();
    cleanup();
    vi.mocked(api.agents.listArchived).mockResolvedValue(archived);
    render(<AgentsPage />);
    await screen.findAllByText("Old bot");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
