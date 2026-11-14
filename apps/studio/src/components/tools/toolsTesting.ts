/**
 * Test helpers for the Tools page. Not imported by app code.
 *
 * A stateful stand-in for `next/navigation`: `push`, `replace` and `back` change an in-memory
 * history (mirrored to `window.location`) and re-render every reader of `usePathname` and
 * `useSearchParams`, the way the app router does, so `?tool=` Sheets open and close for real.
 *
 *   vi.mock("next/navigation", async () => (await import("@/components/tools/toolsTesting")).toolsNavigationModule);
 *   beforeEach(() => toolsNavigation.reset(routes.tool("clock.now", { tab: "run" })));
 */
import { useMemo, useSyncExternalStore } from "react";
import { vi } from "vitest";
import { routes } from "@/lib/routes";

let entries: string[] = [routes.tools()];
let index = 0;
const listeners = new Set<() => void>();

function current(): string {
  return entries[index];
}

function changed() {
  window.history.replaceState(null, "", current());
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const push = vi.fn((href: string) => {
  entries = [...entries.slice(0, index + 1), href];
  index = entries.length - 1;
  changed();
});
const replace = vi.fn((href: string) => {
  entries = entries.map((entry, i) => (i === index ? href : entry));
  changed();
});
const back = vi.fn(() => {
  if (index > 0) index -= 1;
  changed();
});
const noop = () => undefined;

export const toolsNavigation = {
  push,
  replace,
  back,
  /** Starts a fresh history at `url` (a page load) and clears the call records. */
  reset(url = routes.tools()) {
    entries = [url];
    index = 0;
    push.mockClear();
    replace.mockClear();
    back.mockClear();
    window.history.replaceState(null, "", url);
  },
  /** The current URL (path and query). */
  url: current,
  /** The current query. */
  params(): URLSearchParams {
    return new URLSearchParams(current().split("?")[1] ?? "");
  },
};

export const toolsNavigationModule = {
  useRouter: () => ({ push, replace, back, forward: noop, refresh: noop, prefetch: noop }),
  usePathname: () => useSyncExternalStore(subscribe, () => current().split("?")[0]),
  useSearchParams: () => {
    const search = useSyncExternalStore(subscribe, () => current().split("?")[1] ?? "");
    return useMemo(() => new URLSearchParams(search), [search]);
  },
  useParams: () => ({}),
};

const SETTLED_SHEET = ["tool-tab-overview", "tool-not-found", "tool-load-error"].map((id) => `[data-testid="${id}"]`).join(",");

/**
 * The open tool Sheet once it shows its tool (tabs), not-found, or a load error. A `?tool=` link
 * opens the Sheet before the catalog or the agents scan has loaded ("Loading tool…"); queries made
 * right after the Sheet appears would race that load.
 */
export async function findToolSheet(): Promise<HTMLElement> {
  const { screen, waitFor } = await import("@testing-library/react");
  return waitFor(() => {
    const sheet = screen.getByTestId("tool-sheet");
    if (!sheet.querySelector(SETTLED_SHEET)) throw new Error("the tool Sheet is still loading");
    return sheet;
  });
}
