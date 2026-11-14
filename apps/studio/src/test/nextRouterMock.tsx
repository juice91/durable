/**
 * A `next/navigation` stand-in that behaves like the Next 15.5 app router where it matters for URL
 * state: navigations commit asynchronously (a transition), and a new navigation that arrives while a
 * `replace` is still pending DROPS that replace. It keeps a history stack (push adds, replace
 * rewrites the top, back pops) and mirrors the committed URL into `window.location`.
 *
 * Use:
 *   vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);
 *   import { nextRouter } from "@/test/nextRouterMock";
 *   nextRouter.reset(["/agents", "/logs"]);   // history, current entry last
 *   ... await nextRouter.settle();           // let pending navigations commit
 */
import React from "react";

type Pending = { kind: "push" | "replace"; href: string } | { kind: "back" };

const listeners = new Set<() => void>();
const state = {
  entries: ["/"] as string[],
  index: 0,
  pending: [] as Pending[],
  timer: null as ReturnType<typeof setTimeout> | null,
  /** Replaces dropped because a newer navigation arrived while they were pending. */
  dropped: [] as string[],
};

function current(): string {
  return state.entries[state.index];
}

function notify() {
  const href = current();
  try {
    window.history.replaceState(null, "", href);
  } catch {
    // jsdom refuses nothing same-origin; ignore anything odd.
  }
  listeners.forEach((listener) => listener());
}

function commit() {
  state.timer = null;
  const queue = state.pending;
  state.pending = [];
  for (const nav of queue) {
    if (nav.kind === "push") {
      state.entries = state.entries.slice(0, state.index + 1);
      state.entries.push(nav.href);
      state.index = state.entries.length - 1;
    } else if (nav.kind === "replace") {
      state.entries[state.index] = nav.href;
    } else if (state.index > 0) {
      state.index -= 1;
    }
  }
  notify();
}

function enqueue(nav: Pending) {
  // Next 15.5: a navigation arriving while a replace is pending supersedes it (the replace is lost).
  state.pending = state.pending.filter((p) => {
    if (p.kind === "replace") {
      state.dropped.push(p.href);
      return false;
    }
    return true;
  });
  state.pending.push(nav);
  if (!state.timer) state.timer = setTimeout(commit, 0);
}

export const nextRouter = {
  push: (href: string) => enqueue({ kind: "push", href }),
  replace: (href: string) => enqueue({ kind: "replace", href }),
  back: () => enqueue({ kind: "back" }),
  prefetch: () => undefined,
  /** Start over with this history (the last entry is the current one). */
  reset(entries: string[]) {
    if (state.timer) clearTimeout(state.timer);
    state.timer = null;
    state.pending = [];
    state.dropped = [];
    state.entries = [...entries];
    state.index = entries.length - 1;
    notify();
  },
  /** Wait for pending navigations to commit (and React to re-render). */
  async settle() {
    await new Promise((resolve) => setTimeout(resolve, 5));
  },
  get url(): string {
    return current();
  },
  get entries(): readonly string[] {
    return state.entries.slice(0, state.index + 1);
  },
  get dropped(): readonly string[] {
    return state.dropped;
  },
};

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const pathOf = (href: string) => href.split("?")[0];
const searchOf = (href: string) => (href.includes("?") ? href.slice(href.indexOf("?") + 1) : "");

export const navigationModule = {
  useRouter: () => nextRouter,
  usePathname: () => pathOf(React.useSyncExternalStore(subscribe, current, current)),
  useSearchParams: () => {
    const search = searchOf(React.useSyncExternalStore(subscribe, current, current));
    return React.useMemo(() => new URLSearchParams(search), [search]);
  },
  useParams: () => ({}),
  redirect: (href: string) => {
    throw new Error(`redirect(${href})`);
  },
  notFound: () => {
    throw new Error("notFound()");
  },
};
