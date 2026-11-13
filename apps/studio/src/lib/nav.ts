import {
  AgentsIcon,
  DashboardIcon,
  LogsIcon,
  McpIcon,
  MetricsIcon,
  PlaygroundIcon,
  ProcessorsIcon,
  RequestContextIcon,
  SettingsIcon,
  ToolsIcon,
  TracesIcon,
  WorkflowsIcon,
  type IconComponent,
} from "./icons";
import { routes } from "./routes";

/**
 * The single nav registry (decision "Nav registry"). The sidebar, the command palette, the g-key
 * shortcuts, the shortcut help dialog and the breadcrumbs all read it.
 *
 * Groups render in `NAV_GROUP_ORDER`; a group with no items is not rendered. Approvals (AC.15),
 * Knowledge (RM.16) and Evaluation (AC.16) are added by their tasks, never as empty slots.
 */
export type NavGroupId = "approvals" | "primitives" | "evaluation" | "observability" | "settings";

export interface NavGroup {
  id: NavGroupId;
  /** Visible group label; null for the untitled top group. */
  title: string | null;
  /** The bottom group sits in the sidebar footer, not in the scrolling list. */
  placement: "main" | "footer";
}

export const NAV_GROUPS: readonly NavGroup[] = [
  { id: "approvals", title: null, placement: "main" },
  { id: "primitives", title: "Primitives", placement: "main" },
  { id: "evaluation", title: "Evaluation", placement: "main" },
  { id: "observability", title: "Observability", placement: "main" },
  { id: "settings", title: null, placement: "footer" },
];

export const NAV_GROUP_ORDER: readonly NavGroupId[] = NAV_GROUPS.map((group) => group.id);

export interface NavItem {
  /** Stable id (palette, shortcuts, tests). */
  id: string;
  label: string;
  href: string;
  icon: IconComponent;
  group: NavGroupId;
  /** The letter after `g` that opens this page (`g a`: agents). */
  gKey?: string;
  /**
   * Path prefixes that make this item active (the href's path is always one). An item is active on
   * the prefix itself and every sub-path: `/agents` matches `/agents/x/chat`, not `/agents-x`.
   */
  match?: readonly string[];
  /** Active only on exactly its href (the root `/`). */
  exact?: boolean;
  /** A page that a later task replaces and removes from the registry (SL.6, SL.7, SL.10). */
  transitional?: boolean;
  /** Extra palette search words. */
  keywords?: readonly string[];
}

export const navItems: readonly NavItem[] = [
  // Primitives
  { id: "agents", label: "Agents", href: routes.agents(), icon: AgentsIcon, group: "primitives", gKey: "a", keywords: ["assistants"] },
  // Transitional: SL.6 moves chat onto the agent page and removes this item.
  { id: "playground", label: "Playground", href: routes.playground(), icon: PlaygroundIcon, group: "primitives", transitional: true, keywords: ["chat"] },
  { id: "workflows", label: "Workflows", href: routes.workflows(), icon: WorkflowsIcon, group: "primitives", gKey: "w" },
  { id: "tools", label: "Tools", href: routes.tools(), icon: ToolsIcon, group: "primitives", gKey: "o" },
  { id: "mcp", label: "MCP Servers", href: routes.mcp(), icon: McpIcon, group: "primitives", gKey: "m", keywords: ["mcp"] },
  { id: "processors", label: "Processors", href: routes.processors(), icon: ProcessorsIcon, group: "primitives", gKey: "p", keywords: ["guardrails"] },
  // Transitional: SL.10 turns request context into a composer popover and removes this item.
  { id: "request-context", label: "Request Context", href: routes.requestContext(), icon: RequestContextIcon, group: "primitives", transitional: true },
  // Observability
  // Transitional: SL.7 folds the dashboard into Metrics and removes this item.
  { id: "dashboard", label: "Dashboard", href: routes.dashboard(), icon: DashboardIcon, group: "observability", exact: true, transitional: true, keywords: ["overview", "home"] },
  { id: "metrics", label: "Metrics", href: routes.metrics(), icon: MetricsIcon, group: "observability", gKey: "n" },
  // Run pages belong to Traces until SL.12 folds them into the trace drawer.
  { id: "traces", label: "Traces", href: routes.traces(), icon: TracesIcon, group: "observability", gKey: "t", match: ["/runs"] },
  { id: "logs", label: "Logs", href: routes.logs(), icon: LogsIcon, group: "observability", gKey: "l" },
  // Bottom
  { id: "settings", label: "Settings", href: routes.settings(), icon: SettingsIcon, group: "settings", gKey: "," },
];

export interface NavSection {
  group: NavGroup;
  items: NavItem[];
}

/** The non-empty groups in order, each with its items in registry order. */
export function navSectionsFor(items: readonly NavItem[] = navItems): NavSection[] {
  return NAV_GROUPS.map((group) => ({ group, items: items.filter((item) => item.group === group.id) })).filter(
    (section) => section.items.length > 0,
  );
}

export const navSections: NavSection[] = navSectionsFor();

/** Every item, in sidebar order. */
export const allNavItems: NavItem[] = navSections.flatMap((section) => section.items);

function pathOf(href: string): string {
  return href.split(/[?#]/)[0] || "/";
}

function underPrefix(pathname: string, prefix: string): boolean {
  if (prefix === "/") return pathname === "/";
  const base = prefix.endsWith("/") ? prefix.slice(0, -1) : prefix;
  return pathname === base || pathname.startsWith(`${base}/`);
}

/** Whether `item` is the current section for `pathname` (prefix match; `exact` items match only themselves). */
export function isNavItemActive(item: NavItem, pathname: string | null | undefined): boolean {
  if (!pathname) return false;
  const path = pathOf(pathname);
  const href = pathOf(item.href);
  if (item.exact) return path === href;
  return [href, ...(item.match ?? [])].some((prefix) => underPrefix(path, prefix));
}

/** The active item for `pathname`: the longest matching prefix wins, so nested sections stay unambiguous. */
export function activeNavItem(pathname: string | null | undefined, items: readonly NavItem[] = navItems): NavItem | null {
  let best: NavItem | null = null;
  let bestLength = -1;
  for (const item of items) {
    if (!isNavItemActive(item, pathname)) continue;
    const length = Math.max(...[pathOf(item.href), ...(item.match ?? [])].map((prefix) => (pathname && underPrefix(pathOf(pathname), prefix) ? prefix.length : -1)));
    if (length > bestLength) {
      best = item;
      bestLength = length;
    }
  }
  return best;
}

/** The item a `g` + letter shortcut opens, or null. */
export function navItemForGKey(key: string, items: readonly NavItem[] = navItems): NavItem | null {
  return items.find((item) => item.gKey === key) ?? null;
}

export interface NavCrumb {
  label: string;
  href?: string;
}

/**
 * A breadcrumb for a registry section, so page titles and crumbs use the sidebar's label and href.
 * `current: true` is for the page's own title (the last crumb, no link).
 */
export function navCrumb(id: string, { current = false }: { current?: boolean } = {}): NavCrumb {
  const item = navItems.find((candidate) => candidate.id === id);
  if (!item) throw new Error(`Unknown nav item "${id}"`);
  return current ? { label: item.label } : { label: item.label, href: item.href };
}
