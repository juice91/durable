// Archive / unarchive a workflow: the workflow page's More actions menu, the archived banner and the
// workflows list's Active / Archived scope (same behaviour as the agents' agentArchive.test.tsx).
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";

const nav = vi.hoisted(() => ({ search: "", replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/workflows",
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));
vi.mock("@/components/ui/Toaster", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/components/shortcuts/ShortcutsProvider", () => ({ useCreateAction: () => undefined }));

import { routes } from "@/lib/routes";
import { toast } from "@/components/ui/Toaster";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { WorkflowArchiveBlocked, type Workflow } from "@/lib/workflows";
import WorkflowsPage from "@/app/workflows/page";
import { ARCHIVE_DESCRIPTION, WorkflowArchivedBanner, WorkflowArchiveMenu } from "./WorkflowArchiveMenu";

const fetchMock = vi.fn();
const json = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
const wf = (over: Partial<Workflow> = {}): Workflow =>
  ({ id: "w1", name: "Bluesky Post", draft_spec: null, latest_version: 1, archived: false, archived_at: null, ...over }) as Workflow;
const renderMenu = (ui: React.ReactElement) => render(<TooltipProvider>{ui}</TooltipProvider>);

beforeEach(() => {
  vi.clearAllMocks();
  nav.search = "";
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

async function openMenu() {
  const trigger = screen.getByTestId("workflow-more-actions");
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  fireEvent.keyDown(trigger, { key: "Enter" });
  return screen.findByRole("menu");
}

describe("WorkflowArchiveMenu", () => {
  it("archives after confirming and reports the new workflow", async () => {
    const onChanged = vi.fn();
    fetchMock.mockImplementation(() => json(200, { ...wf({ archived: true, archived_at: "2026-11-19T10:00:00Z" }), paused_schedules: [] }));
    renderMenu(<WorkflowArchiveMenu workflow={wf()} onChanged={onChanged} />);
    fireEvent.click(within(await openMenu()).getByText("Archive workflow"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(ARCHIVE_DESCRIPTION);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive workflow" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ archived: true })));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/api/v1/workflows/w1/archive");
    expect(JSON.parse(init.body)).toEqual({ archived: true, force: false });
    expect(toast.success).toHaveBeenCalledWith("Archived Bluesky Post");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("offers Archive anyway with force when runs are active", async () => {
    const onChanged = vi.fn();
    fetchMock
      .mockImplementationOnce(() => json(409, { detail: "workflow has 2 queued or running run(s)", code: "runs_active", active_runs: 2 }))
      .mockImplementationOnce(() => json(200, { ...wf({ archived: true }), paused_schedules: [] }));
    renderMenu(<WorkflowArchiveMenu workflow={wf()} onChanged={onChanged} />);
    fireEvent.click(within(await openMenu()).getByText("Archive workflow"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive workflow" }));
    const note = await within(dialog).findByTestId("workflow-archive-runs");
    expect(note).toHaveTextContent("2 runs are queued or running");
    expect(onChanged).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive anyway" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ archived: true })));
    expect(fetchMock.mock.calls.map((c) => JSON.parse(c[1].body).force)).toEqual([false, true]);
  });

  it("keeps the dialog open with the error when archiving fails", async () => {
    const onChanged = vi.fn();
    fetchMock.mockImplementation(() => json(500, { detail: "boom" }));
    renderMenu(<WorkflowArchiveMenu workflow={wf()} onChanged={onChanged} />);
    fireEvent.click(within(await openMenu()).getByText("Archive workflow"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive workflow" }));
    expect(await within(dialog).findByText("Could not archive the workflow: boom")).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("an archived workflow's menu offers Unarchive, without a confirmation", async () => {
    const onChanged = vi.fn();
    fetchMock.mockImplementation(() => json(200, { ...wf(), paused_schedules: [] }));
    renderMenu(<WorkflowArchiveMenu workflow={wf({ archived: true })} onChanged={onChanged} />);
    const menu = await openMenu();
    expect(within(menu).queryByText("Archive workflow")).toBeNull();
    fireEvent.click(within(menu).getByText("Unarchive workflow"));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ archived: false })));
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ archived: false });
    expect(toast.success).toHaveBeenCalledWith("Unarchived Bluesky Post");
  });
});

describe("WorkflowArchivedBanner", () => {
  it("explains the state, links to the archived list, and unarchives", async () => {
    const onChanged = vi.fn();
    fetchMock.mockImplementation(() => json(200, { ...wf(), paused_schedules: [] }));
    render(<WorkflowArchivedBanner workflow={wf({ archived: true })} onChanged={onChanged} />);
    const banner = screen.getByTestId("workflow-archived-banner");
    expect(banner).toHaveTextContent("This workflow is archived");
    expect(banner).toHaveTextContent("Its schedules stay paused");
    expect(within(banner).getByRole("link", { name: "Archived workflows" })).toHaveAttribute("href", routes.workflows({ archived: true }));
    fireEvent.click(within(banner).getByRole("button", { name: "Unarchive" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ archived: false })));
  });
});

describe("WorkflowArchiveBlocked", () => {
  it("is an ApiError with the active run count", () => {
    const error = new WorkflowArchiveBlocked("busy", 3);
    expect(error.status).toBe(409);
    expect(error.activeRuns).toBe(3);
  });
});

describe("/workflows scope", () => {
  const routeList = (url: string) => {
    const u = new URL(url);
    if (u.pathname === "/api/v1/workflows") {
      const archived = u.searchParams.get("archived");
      return json(200, archived === "true" ? [wf({ id: "w9", name: "Retired", archived: true })] : [wf()]);
    }
    if (u.pathname === "/api/v1/runs") return json(200, { items: [], next_before: null });
    return json(404, { detail: "not mocked" });
  };

  it("hides archived workflows by default and shows them in the Archived scope", async () => {
    fetchMock.mockImplementation(routeList);
    const { unmount } = render(<WorkflowsPage />);
    expect(await screen.findByTestId("workflow-row-w1")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-row-w9")).toBeNull();
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("archived="))).toBe(false);
    fireEvent.click(screen.getByRole("radio", { name: "Archived" }));
    expect(nav.replace).toHaveBeenCalledWith(routes.workflows({ archived: true }));
    unmount();

    nav.search = "view=archived";
    render(<WorkflowsPage />);
    expect(await screen.findByTestId("workflow-row-w9")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-row-w1")).toBeNull();
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("archived=true"))).toBe(true);
  });
});
