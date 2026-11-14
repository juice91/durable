import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ path: "/agents/a1/editor", push: vi.fn(), replace: vi.fn() }));
// RM.17: the Knowledge and Memory steps load these when shown; never reach the network.
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(() => Promise.resolve([])), listEmbeddingModels: vi.fn(() => Promise.resolve([])) }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => nav.path,
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({ id: "a1" }),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { get: vi.fn(), list: vi.fn(), update: vi.fn() }, versions: { list: vi.fn(), create: vi.fn() } } };
});
vi.mock("@/lib/tools", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/tools")>()), listTools: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/workflows")>()), listWorkflows: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(() => Promise.resolve([])), test: vi.fn() } };
});
vi.mock("@/components/agents/specSections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/agents/specSections")>();
  return { ...actual, fetchProcessorCatalog: vi.fn(() => new Promise(() => undefined)), fetchModels: vi.fn(() => new Promise(() => undefined)) };
});

import { api, type AgentSpec } from "@/lib/api";
import { routes } from "@/lib/routes";
import { mockMatchMedia, setWindowWidth, TEST_WIDE_WIDTH, type MatchMediaControl } from "@/components/frame/frameTesting";
import { PageHeader } from "@/components/frame/PageHeader";
import { confirmNavigation, navigateGuarded, registerNavigationGuard } from "@/components/frame/navigationGuard";
import { KeyboardHarness, pressKey, pressKeys } from "@/components/shortcuts/shortcutsTesting";
import { EditorTab } from "@/components/agentPage/EditorTab";

const DRAFT: AgentSpec = { spec_version: 1, name: "Support bot", instructions: "Help", model: { provider: "test", name: "echo" }, tools: [] };

describe("navigation guard registry", () => {
  it("lets navigation through with no guard, synchronously for navigateGuarded", async () => {
    expect(await confirmNavigation("/x")).toBe(true);
    const push = vi.fn();
    navigateGuarded("/x", push);
    expect(push).toHaveBeenCalledWith("/x");
  });

  it("asks every guard newest first and stops at the first refusal", async () => {
    const calls: string[] = [];
    const older = registerNavigationGuard(() => {
      calls.push("older");
      return false;
    });
    const newer = registerNavigationGuard(() => {
      calls.push("newer");
      return true;
    });
    expect(await confirmNavigation("/x")).toBe(false);
    expect(calls).toEqual(["newer", "older"]);
    calls.length = 0;
    const newest = registerNavigationGuard(() => {
      calls.push("newest");
      return false;
    });
    expect(await confirmNavigation("/x")).toBe(false);
    expect(calls).toEqual(["newest"]);
    newest();
    newer();
    older();
  });

  it("a refusal or a throwing guard blocks", async () => {
    const outer = registerNavigationGuard(() => true);
    const inner = registerNavigationGuard((href) => href !== "/blocked");
    expect(await confirmNavigation("/ok")).toBe(true);
    expect(await confirmNavigation("/blocked")).toBe(false);
    const push = vi.fn();
    navigateGuarded("/blocked", push);
    await Promise.resolve();
    expect(push).not.toHaveBeenCalled();
    inner();
    const throwing = registerNavigationGuard(() => {
      throw new Error("x");
    });
    expect(await confirmNavigation("/any")).toBe(false);
    throwing();
    outer();
    expect(await confirmNavigation("/blocked")).toBe(true);
  });
});

describe("navigation by code goes through the editor's unsaved-changes guard", () => {
  let media: MatchMediaControl;
  let restoreWidth: () => void;
  beforeEach(() => {
    vi.clearAllMocks();
    media = mockMatchMedia(true);
    restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
    vi.mocked(api.agents.get).mockResolvedValue({ id: "a1", name: "Support bot", draft_spec: DRAFT, latest_version: null });
    vi.mocked(api.agents.list).mockResolvedValue([
      { id: "a1", name: "Support bot", draft_spec: DRAFT, latest_version: null },
      { id: "a2", name: "Billing bot", draft_spec: DRAFT, latest_version: null },
    ]);
    vi.mocked(api.versions.list).mockResolvedValue([]);
  });
  afterEach(() => {
    media.restore();
    restoreWidth();
  });

  const switcherHeader = (
    <PageHeader
      crumbs={[
        { label: "Agents", href: routes.agents() },
        {
          label: "Support bot",
          switcher: {
            label: "Switch agent",
            value: "a1",
            options: [
              { value: "a1", label: "Support bot" },
              { value: "a2", label: "Billing bot" },
            ],
            hrefFor: (id) => routes.agentEditor(id),
          },
        },
      ]}
    />
  );

  async function renderDirtyEditor() {
    render(
      <KeyboardHarness>
        {switcherHeader}
        <EditorTab agentId="a1" />
      </KeyboardHarness>,
    );
    const name = await screen.findByLabelText("Name");
    fireEvent.change(name, { target: { value: "Edited" } });
    name.blur();
  }

  async function stayThenLeave(navigate: () => Promise<void> | void, href: string | RegExp) {
    await navigate();
    const dialog = await screen.findByRole("dialog", { name: "Leave without saving?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Stay" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull());
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Name")).toHaveValue("Edited");

    await navigate();
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Leave without saving?" })).getByRole("button", { name: "Leave without saving" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(typeof href === "string" ? href : expect.stringMatching(href)));
  }

  it("the g shortcuts ask first", async () => {
    await renderDirtyEditor();
    await stayThenLeave(() => pressKeys("g", "t"), routes.traces());
  });

  it("the command palette asks first", async () => {
    await renderDirtyEditor();
    await stayThenLeave(async () => {
      pressKey("k", { metaKey: true });
      const input = await screen.findByTestId("command-palette-input");
      fireEvent.change(input, { target: { value: "Metrics" } });
      fireEvent.keyDown(input, { key: "Enter" });
    }, /^\/metrics/);
  });

  it("the breadcrumb agent switcher asks first", async () => {
    await renderDirtyEditor();
    await stayThenLeave(async () => {
      fireEvent.click(screen.getByRole("combobox", { name: "Switch agent" }));
      fireEvent.click(await screen.findByRole("option", { name: /Billing bot/ }));
    }, routes.agentEditor("a2"));
  });

  it("a clean editor does not ask", async () => {
    render(
      <KeyboardHarness>
        <EditorTab agentId="a1" />
      </KeyboardHarness>,
    );
    await screen.findByLabelText("Name");
    pressKeys("g", "t");
    expect(nav.push).toHaveBeenCalledWith(routes.traces());
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });
});
