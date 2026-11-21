/**
 * The threads pane comes back when the room returns, even if the first expand was refused (review I3
 * follow-up). In the browser: 1100 px, Config open (threads auto-collapsed), then `[` collapses the
 * sidebar to the rail. The expand runs while the sidebar is still mid-transition and the group is
 * too narrow for threads + chat, so the kit snaps the pane back to collapsed; nothing retried, and the
 * header still said "pressed". jsdom has no layout, so this file stands in for the kit's refusal:
 * the threads panel's imperative handle is a fake whose `expand()` only works when `room.ok`, and
 * ResizeObserver is a fake the test fires (the group element resizing as the transition ends).
 */
import React from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const nav = vi.hoisted(() => ({ path: "/agents/a1/chat" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => ({ id: "a1" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { get: vi.fn(), list: vi.fn() }, versions: { list: vi.fn() } } };
});
vi.mock("@/lib/metrics", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/metrics")>();
  return { ...original, fetchModels: vi.fn(async () => []) };
});
vi.mock("@/lib/requestContext", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/requestContext")>();
  return { ...original, getRequestContext: vi.fn(async () => null) };
});

// The threads panel's handle: collapsed state kept here; an expand without room is refused.
// `px` is the pane's width; an expand restores `leftoverPx`, standing in for the kit restoring the
// percent it remembered from a group of another width.
const pane = vi.hoisted(() => ({ collapsed: false, room: { ok: true }, expandCalls: 0, px: 280, leftoverPx: 280 }));
// The `onLayoutChanged` the kit's Group receives (ResizableGroup's, which saves user drags and then
// calls the panes'), so a test can report a layout change as a user drag or as an imperative one.
const group = vi.hoisted(() => ({ onLayoutChanged: null as null | ((layout: Record<string, number>, meta: { isUserInteraction: boolean; requestedLayout?: Record<string, number> }) => void) }));
vi.mock("react-resizable-panels", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-resizable-panels")>();
  const ReactModule = await import("react");
  const Group = (props: React.ComponentProps<typeof actual.Group>) => {
    group.onLayoutChanged = (props.onLayoutChanged as typeof group.onLayoutChanged) ?? null;
    return ReactModule.createElement(actual.Group, props);
  };
  return {
    ...actual,
    Group,
    usePanelRef: () =>
      ReactModule.useMemo(() => {
        const handle = {
            collapse: () => {
              pane.collapsed = true;
            },
            expand: () => {
              pane.expandCalls += 1;
              if (pane.room.ok) {
                pane.collapsed = false;
                pane.px = pane.leftoverPx;
              }
            },
            isCollapsed: () => pane.collapsed,
            getSize: () => ({ asPercentage: pane.collapsed ? 0 : 30, inPixels: pane.collapsed ? 0 : pane.px }),
            resize: (size: number | string) => {
              if (typeof size === "number") pane.px = size;
            },
        };
        // The kit assigns its real handle to `current` (useImperativeHandle): keep the fake.
        return Object.defineProperty({}, "current", { get: () => handle, set: () => {}, enumerable: true });
      }, []),
  };
});

// A ResizeObserver the test fires by hand.
const observers = vi.hoisted(() => new Set<{ callback: ResizeObserverCallback; targets: Set<Element> }>());
class FakeResizeObserver {
  private entry: { callback: ResizeObserverCallback; targets: Set<Element> };
  constructor(callback: ResizeObserverCallback) {
    this.entry = { callback, targets: new Set() };
    observers.add(this.entry);
  }
  observe(target: Element) {
    this.entry.targets.add(target);
  }
  unobserve(target: Element) {
    this.entry.targets.delete(target);
  }
  disconnect() {
    observers.delete(this.entry);
  }
}
function resize(element: Element) {
  for (const entry of Array.from(observers)) {
    if (entry.targets.has(element)) entry.callback([], entry as unknown as ResizeObserver);
  }
}

import { api, type Agent } from "@/lib/api";
import { FrameHarness, mockMatchMedia, setWindowWidth, type MatchMediaControl } from "@/components/frame/frameTesting";
import { AgentLayout } from "./AgentLayout";
import { AgentChatPanes } from "./AgentChatPanes";
import { agentLayoutKey, agentThreadsWidthKey, readConfigOpen } from "./agentPageModel";

const AGENT: Agent = {
  id: "a1",
  name: "Support bot",
  draft_spec: { spec_version: 1, name: "Support bot", instructions: "Help", model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [] },
  latest_version: null,
} as Agent;

let media: MatchMediaControl;
let restoreWidth: () => void;
beforeEach(() => {
  window.localStorage.clear();
  pane.collapsed = false;
  pane.room.ok = true;
  pane.expandCalls = 0;
  pane.px = 280;
  pane.leftoverPx = 280;
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  restoreWidth = setWindowWidth(1100);
  media = mockMatchMedia(true);
  vi.mocked(api.agents.get).mockResolvedValue(AGENT);
  vi.mocked(api.agents.list).mockResolvedValue([AGENT]);
  vi.mocked(api.versions.list).mockResolvedValue([]);
});
afterEach(() => {
  cleanup();
  media.restore();
  restoreWidth();
  vi.unstubAllGlobals();
  vi.restoreAllMocks(); // vitest 4 keeps a spy across tests until it is restored
  observers.clear();
  group.onLayoutChanged = null;
  document.documentElement.removeAttribute("data-sidebar");
});

it("a refused expand reads as not pressed, and is retried when the group resizes", async () => {
  render(
    <FrameHarness>
      <AgentLayout agentId="a1">
        <AgentChatPanes agentId="a1" threads={<nav aria-label="Conversations" />}>
          <p>chat</p>
        </AgentChatPanes>
      </AgentLayout>
    </FrameHarness>,
  );
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  const group = await screen.findByTestId("agent-chat-panes");
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));

  // Config open at 1100 px with the expanded sidebar: threads + chat do not fit, so threads collapse.
  fireEvent.keyDown(document.body, { key: "]" });
  await screen.findByRole("complementary", { name: "Config" });
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "false"));
  expect(pane.collapsed).toBe(true);

  // `[`: the rail makes room, but the group has not widened yet: the expand is refused.
  pane.room.ok = false;
  fireEvent.keyDown(document.body, { key: "[" });
  await waitFor(() => expect(pane.expandCalls).toBeGreaterThan(0));
  expect(pane.collapsed).toBe(true);
  // The header follows the real pane, not the wish.
  expect(toggle).toHaveAttribute("aria-pressed", "false");

  // The sidebar transition ends; the group's new size arrives: the expand is retried and succeeds.
  pane.room.ok = true;
  await act(async () => {
    resize(group);
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
  await waitFor(() => expect(pane.collapsed).toBe(false));
  expect(toggle).toHaveAttribute("aria-pressed", "true");
  // Neither the auto-collapse nor its undoing was saved as the user's layout.
  expect(window.localStorage.getItem("studio.agent-layout.v1.a1")).toBeNull();
});

function Page({ panes = true }: { panes?: boolean }) {
  return (
    <FrameHarness>
      <AgentLayout agentId="a1">
        {panes ? (
          <AgentChatPanes agentId="a1" threads={<nav aria-label="Conversations" />}>
            <p>chat</p>
          </AgentChatPanes>
        ) : (
          <p>no panes</p>
        )}
      </AgentLayout>
    </FrameHarness>
  );
}

async function settle(element?: Element) {
  await act(async () => {
    if (element) resize(element);
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

const storedLayout = () => window.localStorage.getItem(agentLayoutKey("a1"));

it("a user drag that collapses the pane is saved and becomes the user's choice", async () => {
  render(<Page />);
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  const panes = await screen.findByTestId("agent-chat-panes");
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));
  expect(group.onLayoutChanged).not.toBeNull();

  pane.collapsed = true;
  act(() => group.onLayoutChanged?.({ threads: 0, chat: 100 }, { isUserInteraction: true, requestedLayout: { threads: 0, chat: 100 } }));
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "false"));
  expect(JSON.parse(storedLayout() ?? "{}")).toEqual({ threads: 0, chat: 100 });
  // The user's choice is now "collapsed": a later resync leaves the pane collapsed.
  await settle(panes);
  expect(pane.collapsed).toBe(true);
  expect(toggle).toHaveAttribute("aria-pressed", "false");
});

it("an imperative layout change is neither saved nor taken as the user's choice", async () => {
  render(<Page />);
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  const panes = await screen.findByTestId("agent-chat-panes");
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));

  pane.collapsed = true;
  act(() => group.onLayoutChanged?.({ threads: 0, chat: 100 }, { isUserInteraction: false }));
  expect(storedLayout()).toBeNull();
  // The user still wants threads: the next resync expands the pane again.
  await settle(panes);
  expect(pane.collapsed).toBe(false);
  expect(toggle).toHaveAttribute("aria-pressed", "true");
  expect(storedLayout()).toBeNull();
});

it("a toggle during a refused expand is saved only once the pane really shows it", async () => {
  render(<Page />);
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  const panes = await screen.findByTestId("agent-chat-panes");
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));
  // The user collapses the threads (saved), then asks for them back while the layout refuses.
  fireEvent.keyDown(document.body, { key: "{" });
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "false"));
  await waitFor(() => expect(JSON.parse(storedLayout() ?? "{}").threads).toBe(0));
  const collapsedLayout = storedLayout();
  pane.room.ok = false;
  fireEvent.keyDown(document.body, { key: "{" });
  await waitFor(() => expect(pane.expandCalls).toBeGreaterThan(0));
  expect(pane.collapsed).toBe(true);
  // Nothing is saved while the pane is still collapsed: the stored layout is untouched.
  await settle();
  expect(storedLayout()).toBe(collapsedLayout);
  pane.room.ok = true;
  await settle(panes);
  expect(pane.collapsed).toBe(false);
  expect(toggle).toHaveAttribute("aria-pressed", "true");
  // Saved now, as open: the stored collapse is gone.
  const saved = storedLayout();
  if (saved !== null) expect(JSON.parse(saved).threads).not.toBe(0);
});

it("the panes report null on unmount, so the header falls back to the stored state", async () => {
  const view = render(<Page />);
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  const panes = await screen.findByTestId("agent-chat-panes");
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));
  // The layout refuses the pane: the header shows the real, collapsed pane.
  pane.collapsed = true;
  pane.room.ok = false;
  await settle(panes);
  expect(toggle).toHaveAttribute("aria-pressed", "false");
  // Without the panes there is no real pane to show: the stored state (open) again.
  view.rerender(<Page panes={false} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Conversations" })).toHaveAttribute("aria-pressed", "true"));
});

it("asking for threads while the layout refuses them, with Config open, closes Config (not saved)", async () => {
  // 1440 px: the numbers say it fits, so Config opens and nothing is auto-collapsed; the real layout
  // still refuses the pane (a scrollbar's width short, say).
  restoreWidth();
  restoreWidth = setWindowWidth(1440);
  render(<Page />);
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  const panes = await screen.findByTestId("agent-chat-panes");
  await screen.findByRole("complementary", { name: "Config" });
  pane.collapsed = true;
  pane.room.ok = false;
  await settle(panes);
  expect(toggle).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(toggle);
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull());
  // The close was made for the user: their per-agent Config choice is not saved as closed.
  expect(readConfigOpen("a1")).toBeNull();
  pane.room.ok = true;
  await settle(panes);
  expect(toggle).toHaveAttribute("aria-pressed", "true");
});

it("an expand after an auto-collapse restores the user's px width, never the kit's leftover; toggles save open/closed only", async () => {
  render(<Page />);
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  const panes = await screen.findByTestId("agent-chat-panes");
  // The group has a size (jsdom does no layout): 774 px with Config closed.
  vi.spyOn(panes, "getBoundingClientRect").mockReturnValue({ width: 774, height: 600, x: 0, y: 0, top: 0, left: 0, right: 774, bottom: 600, toJSON: () => ({}) } as DOMRect);
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));

  // 1100 px: `]` opens Config and auto-collapses threads.
  fireEvent.keyDown(document.body, { key: "]" });
  await screen.findByRole("complementary", { name: "Config" });
  await waitFor(() => expect(pane.collapsed).toBe(true));
  // The kit would bring the pane back at the separator's maximum (57.6 % of 849 px).
  pane.leftoverPx = 489;
  fireEvent.click(toggle);
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull());
  await waitFor(() => expect(pane.collapsed).toBe(false));
  expect(pane.px).toBe(280);
  expect(toggle).toHaveAttribute("aria-pressed", "true");
  // Nothing saved: neither the transient layout nor a width.
  expect(storedLayout()).toBeNull();
  expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBeNull();

  // A real resize to 39 %: the saved px is the layout's threads % x the group's width (774 px, no
  // separator width in jsdom), not the DOM's width, which still shows the previous layout (280).
  act(() => group.onLayoutChanged?.({ threads: 39, chat: 61 }, { isUserInteraction: true, requestedLayout: { threads: 39, chat: 61 } }));
  expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBe(String(Math.round(0.39 * 774)));
  expect(JSON.parse(storedLayout() ?? "{}")).toEqual({ threads: 39, chat: 61 });
  // A second step (a separator key): the saved px follows this step, not the one before.
  act(() => group.onLayoutChanged?.({ threads: 41, chat: 59 }, { isUserInteraction: true, requestedLayout: { threads: 41, chat: 59 } }));
  expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBe(String(Math.round(0.41 * 774)));
  const userPx = Math.round(0.41 * 774);

  // `{` closes (saved as closed), `{` reopens at the user's width whatever the kit restores; the width stays saved.
  fireEvent.keyDown(document.body, { key: "{" });
  await waitFor(() => expect(JSON.parse(storedLayout() ?? "{}").threads).toBe(0));
  pane.leftoverPx = 489;
  fireEvent.keyDown(document.body, { key: "{" });
  await waitFor(() => expect(pane.collapsed).toBe(false));
  expect(pane.px).toBe(userPx);
  await waitFor(() => expect(storedLayout()).toBeNull());
  expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBe(String(userPx));

  // The group resizing later (the window, the sidebar) keeps the px width.
  pane.px = 420;
  await settle(panes);
  expect(pane.px).toBe(userPx);
});

it("a layout saved before px widths (percent only) migrates once: its width is saved in px and survives a collapse", async () => {
  // A user who dragged threads to 40 % before px widths were kept; no `:threads-px` yet.
  window.localStorage.setItem(agentLayoutKey("a1"), JSON.stringify({ threads: 40, chat: 60 }));
  const rect = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.testid === "agent-chat-panes") return { width: 774, height: 600, x: 0, y: 0, top: 0, left: 0, right: 774, bottom: 600, toJSON: () => ({}) } as DOMRect;
    return rect.call(this);
  });
  render(<Page />);
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));
  const migrated = Math.round(0.4 * 774);
  await waitFor(() => expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBe(String(migrated)));

  // `{` collapses (the percent layout becomes threads 0), `{` reopens at the migrated width.
  fireEvent.keyDown(document.body, { key: "{" });
  await waitFor(() => expect(JSON.parse(storedLayout() ?? "{}").threads).toBe(0));
  pane.leftoverPx = 489;
  fireEvent.keyDown(document.body, { key: "{" });
  await waitFor(() => expect(pane.collapsed).toBe(false));
  expect(pane.px).toBe(migrated);
  expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBe(String(migrated));
});

function mockGroupWidth(width: number) {
  const rect = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.testid === "agent-chat-panes") return { width, height: 600, x: 0, y: 0, top: 0, left: 0, right: width, bottom: 600, toJSON: () => ({}) } as DOMRect;
    return rect.call(this);
  });
}

it("the migration never overwrites a px width the user already has", async () => {
  window.localStorage.setItem(agentLayoutKey("a1"), JSON.stringify({ threads: 40, chat: 60 }));
  window.localStorage.setItem(agentThreadsWidthKey("a1"), "333");
  mockGroupWidth(774);
  render(<Page />);
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));
  await settle();
  expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBe("333");
});

it("a layout saved with the pane collapsed (threads 0) has no width to migrate: nothing is written", async () => {
  window.localStorage.setItem(agentLayoutKey("a1"), JSON.stringify({ threads: 0, chat: 100 }));
  mockGroupWidth(774);
  render(<Page />);
  await screen.findByRole("button", { name: "Conversations" });
  await settle();
  expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBeNull();
});

it("no saved layout: nothing is written until the user drags (the default is not a choice)", async () => {
  render(<Page />);
  const toggle = await screen.findByRole("button", { name: "Conversations" });
  await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));
  await settle();
  expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBeNull();
});

function sizedPanes(width: number) {
  // vitest 4 returns the existing spy when a method is spied twice, so restore before capturing the original.
  vi.restoreAllMocks();
  const rect = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.testid === "agent-chat-panes") return { width, height: 600, x: 0, y: 0, top: 0, left: 0, right: width, bottom: 600, toJSON: () => ({}) } as DOMRect;
    return rect.call(this);
  });
}

it("the migrated width is clamped: never so wide that the chat drops under its minimum", async () => {
  window.localStorage.setItem(agentLayoutKey("a1"), JSON.stringify({ threads: 90, chat: 10 }));
  sizedPanes(774);
  render(<Page />);
  // 90 % of 774 px would leave the chat 77 px; the chat keeps its 360 px minimum.
  await waitFor(() => expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBe(String(774 - 360)));
});

it("auto-collapsed on the first visit: the migration waits until threads are shown", async () => {
  window.localStorage.setItem(agentLayoutKey("a1"), JSON.stringify({ threads: 40, chat: 60 }));
  // Config left open at 1100 px crowds threads out (auto-collapsed, not the user's choice).
  window.localStorage.setItem("studio.agent-config.v1.a1", "open");
  sizedPanes(774);
  render(<Page />);
  await screen.findByRole("complementary", { name: "Config" });
  await settle();
  expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBeNull();
  // `]` closes Config: threads come back, and the stored percent migrates then.
  fireEvent.keyDown(document.body, { key: "]" });
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull());
  await waitFor(() => expect(window.localStorage.getItem(agentThreadsWidthKey("a1"))).toBe(String(Math.round(0.4 * 774))));
});
