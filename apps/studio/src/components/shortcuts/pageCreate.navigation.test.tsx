import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// `c` through the real root providers after a client-side navigation: the frame (and so the
// provider's window listener) mounts first, the page second. The bug this guards: a page-local
// listener ran after the provider's, saw the event already prevented, and `c` did nothing.

const nav = vi.hoisted(() => ({ push: vi.fn(), path: "/metrics" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => ({}),
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return {
    ...actual,
    mcpServers: { list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), setEnabled: vi.fn(), remove: vi.fn(), test: vi.fn() },
  };
});

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, agents: { ...actual.api.agents, list: vi.fn() }, versions: { ...actual.api.versions, get: vi.fn() } } };
});

import AgentsPage from "@/app/agents/page";
import McpServersPage from "@/app/mcp/page";
import { mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";
import { api } from "@/lib/api";
import { mcpServers } from "@/lib/mcpServers";
import { routes } from "@/lib/routes";
import { KeyboardHarness, pressKey, pressKeys } from "./shortcutsTesting";

let media: MatchMediaControl;
beforeEach(() => {
  media = mockMatchMedia(true);
  nav.push.mockClear();
  vi.mocked(mcpServers.list).mockResolvedValue([]);
  vi.mocked(api.agents.list).mockResolvedValue([]);
});
afterEach(() => {
  media.restore();
  vi.useRealTimers();
});

/** Mounts the frame with no page, then navigates (client side) to `page`. */
function navigateTo(page: React.ReactElement, path: string) {
  const view = render(<KeyboardHarness />);
  nav.path = path;
  act(() => view.rerender(<KeyboardHarness>{page}</KeyboardHarness>));
  return view;
}

describe("c after a client-side navigation", () => {
  test("/mcp: c opens the Add server menu", async () => {
    navigateTo(<McpServersPage />, routes.mcp());
    await waitFor(() => expect(mcpServers.list).toHaveBeenCalled());
    pressKey("c");
    expect(await screen.findByRole("menuitem", { name: "Remote server by URL" })).toBeInTheDocument();
  });

  test("/mcp: g then c follows the sequence rules (drops c), and a later c still opens the Add menu", async () => {
    navigateTo(<McpServersPage />, routes.mcp());
    await waitFor(() => expect(mcpServers.list).toHaveBeenCalled());
    pressKeys("g", "c");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(nav.push).not.toHaveBeenCalled();
    pressKey("c");
    expect(await screen.findByRole("menuitem", { name: "Remote server by URL" })).toBeInTheDocument();
  });

  test("/agents: c opens New agent", async () => {
    navigateTo(<AgentsPage />, routes.agents());
    await waitFor(() => expect(api.agents.list).toHaveBeenCalled());
    pressKey("c");
    expect(nav.push).toHaveBeenCalledWith(routes.agentNew());
  });
});

test("a c with no create action on the page is not prevented", () => {
  render(<KeyboardHarness />);
  const event = new KeyboardEvent("keydown", { key: "c", bubbles: true, cancelable: true });
  document.body.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(false);
});
