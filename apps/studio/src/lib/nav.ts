import {
  AgentsIcon,
  ArchitectIcon,
  ApprovalsIcon,
  DatasetsIcon,
  ExperimentsIcon,
  KnowledgeIcon,
  LogsIcon,
  McpIcon,
  MetricsIcon,
  ProcessorsIcon,
  ScorersIcon,
  SettingsIcon,
  ThreadsIcon,
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
 * Knowledge (RM.16) and Evaluation (W1.5) are added by their tasks, never as empty slots.
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
  /** Active only on exactly its href, not on its sub-paths. */
  exact?: boolean;
  /** A page that a later task replaces and removes from the registry (none is left since SL.10). */
  transitional?: boolean;
  /** Extra palette search words. */
  keywords?: readonly string[];
  /**
   * A live count shown next to the item (AC.15): the sidebar reads it from its source (see
   * `useNavBadges` in `components/frame/Sidebar.tsx`), shows it in the expanded sidebar, as a dot in the 56 px rail
   * and on the mobile menu button, and hides it at 0.
   */
  badge?: NavBadgeId;
}

/** The live counts a nav item can show. `approvals_pending`: `GET /approvals?status=pending`, polled every 30 s. */
export type NavBadgeId = "approvals_pending";

export const navItems: readonly NavItem[] = [
  // Approvals (AC.15): the untitled top group
  { id: "approvals", label: "Approvals", href: routes.approvals(), icon: ApprovalsIcon, group: "approvals", gKey: "i", badge: "approvals_pending", keywords: ["inbox", "pending", "human in the loop", "approve", "deny"] },
  // Primitives
  { id: "agents", label: "Agents", href: routes.agents(), icon: AgentsIcon, group: "primitives", gKey: "a", keywords: ["assistants"] },
  { id: "architect", label: "Architect", href: routes.architect(), icon: ArchitectIcon, group: "primitives", gKey: "r", keywords: ["create agent", "describe", "build", "generate", "draft"] },
  { id: "workflows", label: "Workflows", href: routes.workflows(), icon: WorkflowsIcon, group: "primitives", gKey: "w" },
  { id: "tools", label: "Tools", href: routes.tools(), icon: ToolsIcon, group: "primitives", gKey: "o" },
  { id: "mcp", label: "MCP Servers", href: routes.mcp(), icon: McpIcon, group: "primitives", gKey: "m", keywords: ["mcp"] },
  { id: "processors", label: "Processors", href: routes.processors(), icon: ProcessorsIcon, group: "primitives", gKey: "p", keywords: ["guardrails"] },
  // RM.16
  { id: "knowledge", label: "Knowledge", href: routes.knowledge(), icon: KnowledgeIcon, group: "primitives", gKey: "k", keywords: ["knowledge bases", "rag", "documents", "retrieval"] },
  // Evaluation (W1.5): evals across every agent of the tenant
  { id: "datasets", label: "Test suites", href: routes.datasets(), icon: DatasetsIcon, group: "evaluation", gKey: "d", keywords: ["datasets", "eval suites", "cases", "evals"] },
  { id: "scorers", label: "Scorers", href: routes.scorers(), icon: ScorersIcon, group: "evaluation", gKey: "s", keywords: ["evaluators", "judges", "scores", "metrics"] },
  { id: "experiments", label: "Experiments", href: routes.experiments(), icon: ExperimentsIcon, group: "evaluation", gKey: "e", keywords: ["eval runs", "runs", "results", "compare"] },
  // Observability
  // SL.7 folded the dashboard into Metrics' Overview header; its palette words point here.
  { id: "metrics", label: "Metrics", href: routes.metrics(), icon: MetricsIcon, group: "observability", gKey: "n", keywords: ["overview", "dashboard", "home"] },
  { id: "threads", label: "Threads", href: routes.threads(), icon: ThreadsIcon, group: "observability", gKey: "h", keywords: ["conversations", "chats", "messages", "history"] },
  { id: "traces", label: "Traces", href: routes.traces(), icon: TracesIcon, group: "observability", gKey: "t", match: [routes.spans()] },
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
