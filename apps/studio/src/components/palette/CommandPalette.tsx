"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Command as CommandPrimitive, defaultFilter, useCommandState } from "cmdk";
import { Check, Keyboard, ListTree, Monitor, Moon, PanelLeft, Plus, Settings, Sun } from "lucide-react";
import { AgentsIcon, ICON_STROKE_WIDTH, McpIcon, ToolsIcon, WorkflowsIcon, type IconComponent } from "@/lib/icons";
import { allNavItems } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { gKeyHint, isPaletteChord } from "@/lib/shortcuts";
import type { ThemePreference } from "@/lib/theme";
import { isModalOpen } from "@/components/frame/keyboard";
import { useFrame } from "@/components/frame/FrameContext";
import { useShortcuts } from "@/components/shortcuts/ShortcutsProvider";
import { useTheme } from "@/components/theme/ThemeProvider";
import { Command, CommandGroup, CommandInput, CommandItem, CommandList, CommandShortcut } from "@/components/ui/Command";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/Dialog";
import { VisuallyHidden } from "@/components/ui/VisuallyHidden";
import {
  ENTITY_SOURCES,
  loadEntities,
  cachedEntities,
  PALETTE_ENTITY_LIMIT,
  PALETTE_SCOPES,
  runIdFromQuery,
  scopeShows,
  type EntityKind,
  type PaletteEntity,
  type PaletteScope,
} from "./paletteData";

type SourceState =
  | { status: "loading" }
  | { status: "ready"; items: PaletteEntity[] }
  | { status: "error"; message: string };

type SourceStates = Partial<Record<EntityKind, SourceState>>;

const ENTITY_ICONS: Record<EntityKind, IconComponent> = {
  agents: AgentsIcon,
  workflows: WorkflowsIcon,
  tools: ToolsIcon,
  mcp: McpIcon,
};

const THEME_COMMANDS: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: "system", label: "System", icon: Monitor },
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
];

interface PaletteRow {
  /** cmdk value: unique and stable, never searched. The search scores `keywords` only (keywordFilter). */
  value: string;
  keywords: string[];
  icon: React.ReactNode;
  label: string;
  detail?: React.ReactNode;
  shortcut?: string | null;
  onSelect: () => void;
  testId?: string;
  attrs: Record<string, string>;
}

interface PaletteSection {
  id: string;
  title: string;
  rows: PaletteRow[];
}

function renderRow(row: PaletteRow) {
  return (
    <CommandItem key={row.value} value={row.value} keywords={row.keywords} onSelect={row.onSelect} data-testid={row.testId} {...row.attrs}>
      {row.icon}
      <span className="min-w-0 truncate">{row.label}</span>
      {row.detail ? <span className="ml-auto min-w-0 truncate text-caption text-muted-foreground">{row.detail}</span> : null}
      {row.shortcut ? <CommandShortcut className={row.detail ? "ml-2" : undefined}>{row.shortcut}</CommandShortcut> : null}
    </CommandItem>
  );
}

/** The keys cmdk acts on (vim bindings are off). */
const CMDK_KEYS = new Set(["Enter", "ArrowUp", "ArrowDown", "Home", "End"]);

/** Scores the row's keywords only: values are ids, which must not match ("45" is not agent "…4562…"). */
function keywordFilter(_value: string, search: string, keywords?: string[]): number {
  return defaultFilter(keywords?.join(" ") ?? "", search.trim());
}

/**
 * The palette's one live region, always mounted so changes are announced: the result count, then
 * loading and load failures. The visible loading and error rows are plain text.
 */
function PaletteAnnouncer({ loading, failures }: { loading: boolean; failures: string[] }) {
  const count = useCommandState((state) => state.filtered.count);
  const parts = [count === 0 ? "No results." : `${count} ${count === 1 ? "result" : "results"}.`];
  if (loading) parts.push("Loading more results.");
  for (const noun of failures) parts.push(`Couldn't load ${noun}.`);
  return (
    <div role="status" aria-live="polite" data-testid="command-palette-announcement" className="sr-only">
      {parts.join(" ")}
    </div>
  );
}

const settingsItem = allNavItems.find((item) => item.id === "settings");
const settingsHint = settingsItem ? gKeyHint(settingsItem) : null;

const iconProps = { className: "size-icon shrink-0 text-muted-foreground", strokeWidth: ICON_STROKE_WIDTH, "aria-hidden": true } as const;

/**
 * The ⌘K / Ctrl+K command palette (decision "Command palette"): pages from the nav registry,
 * agents, workflows, tools and MCP servers (fetched on open, cached 60 s), commands, and "Open trace
 * by run id" when the query is a UUID. Scopes narrow the list. Registers its opener with the frame,
 * so the search buttons in the sidebar and the mobile top bar appear and open it.
 */
export function CommandPalette() {
  const frame = useFrame();
  const shortcuts = useShortcuts();
  const router = useRouter();
  const { theme, setTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<PaletteScope>("all");
  const [sources, setSources] = useState<SourceStates>({});
  const openRef = useRef(open);
  openRef.current = open;
  // Each open starts a new load; results of an older one are dropped.
  const loadToken = useRef(0);

  const show = useCallback(() => {
    setQuery("");
    setScope("all");
    setOpen(true);
  }, []);

  const register = frame?.registerPalette;
  useEffect(() => register?.(show), [register, show]);

  // ⌘K / Ctrl+K toggles the palette from anywhere, fields included, but never on top of another dialog.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented || !isPaletteChord(event)) return;
      // Always: the browser's own Ctrl+K (focus the address bar) must never run, open or not.
      event.preventDefault();
      if (openRef.current) {
        setOpen(false);
        return;
      }
      if (isModalOpen()) return;
      show();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [show]);

  // Fetch entities on open: each source on its own, so one failure leaves the rest (and the pages) usable.
  useEffect(() => {
    if (!open) return;
    const token = ++loadToken.current;
    const initial: SourceStates = {};
    for (const source of ENTITY_SOURCES) {
      const cached = cachedEntities(source.kind);
      initial[source.kind] = cached ? { status: "ready", items: cached } : { status: "loading" };
    }
    setSources(initial);
    for (const source of ENTITY_SOURCES) {
      if (initial[source.kind]?.status === "ready") continue;
      loadEntities(source).then(
        (items) => {
          if (loadToken.current === token) setSources((current) => ({ ...current, [source.kind]: { status: "ready", items } }));
        },
        (error: unknown) => {
          const message = error instanceof Error && error.message ? error.message : "Request failed";
          if (loadToken.current === token) setSources((current) => ({ ...current, [source.kind]: { status: "error", message } }));
        },
      );
    }
  }, [open]);

  const go = useCallback(
    (href: string) => {
      setOpen(false);
      router.push(href);
    },
    [router],
  );

  const runCommand = useCallback((action: () => void) => {
    setOpen(false);
    action();
  }, []);

  const toggleSidebar = useCallback(() => {
    if (!frame) return;
    if (frame.isWide) frame.toggleSidebar();
    else frame.setNavOpen(true);
  }, [frame]);

  const openHelp = useCallback(() => {
    setOpen(false);
    // After the palette has closed and returned focus, so the help dialog's focus trap starts clean.
    window.setTimeout(() => shortcuts?.openHelp(), 0);
  }, [shortcuts]);

  const runId = runIdFromQuery(query);
  const loading = ENTITY_SOURCES.some((source) => scopeShows(scope, source.kind) && sources[source.kind]?.status === "loading");
  const failures = ENTITY_SOURCES.filter((source) => scopeShows(scope, source.kind) && sources[source.kind]?.status === "error");

  const searching = query.trim() !== "";
  const capped: { kind: EntityKind; total: number; noun: string }[] = [];
  const sections: PaletteSection[] = [];
  if (runId && scopeShows(scope, "commands")) {
    sections.push({
      id: "run",
      title: "Run",
      rows: [
        {
          value: "open-trace-by-run-id",
          keywords: [runId, "trace", "run"],
          icon: <ListTree {...iconProps} />,
          label: "Open trace by run id",
          detail: <span className="font-mono">{runId}</span>,
          onSelect: () => go(routes.trace(runId)),
          testId: "palette-open-trace",
          attrs: {},
        },
      ],
    });
  }
  if (scopeShows(scope, "pages")) {
    sections.push({
      id: "pages",
      title: "Pages",
      rows: allNavItems.map((item) => {
        const Icon = item.icon;
        return {
          value: `page:${item.id}`,
          keywords: [item.label, ...(item.keywords ?? [])],
          icon: <Icon {...iconProps} />,
          label: item.label,
          shortcut: gKeyHint(item),
          onSelect: () => go(item.href),
          attrs: { "data-palette-page": item.id },
        };
      }),
    });
  }
  for (const source of ENTITY_SOURCES) {
    const state = sources[source.kind];
    if (!scopeShows(scope, source.kind) || state?.status !== "ready" || state.items.length === 0) continue;
    const Icon = ENTITY_ICONS[source.kind];
    const shown = searching ? state.items : state.items.slice(0, PALETTE_ENTITY_LIMIT);
    if (shown.length < state.items.length) capped.push({ kind: source.kind, total: state.items.length, noun: source.noun });
    sections.push({
      id: source.kind,
      title: source.title,
      rows: shown.map((entity) => ({
        value: `${entity.kind}:${entity.id}`,
        keywords: [entity.label],
        icon: <Icon {...iconProps} />,
        label: entity.label,
        detail: entity.detail,
        onSelect: () => go(entity.href),
        attrs: { "data-palette-entity": `${entity.kind}:${entity.id}` },
      })),
    });
  }
  if (scopeShows(scope, "commands")) {
    sections.push({
      id: "commands",
      title: "Commands",
      rows: [
        {
          value: "command:toggle-sidebar",
          keywords: ["Toggle sidebar", "navigation", "collapse", "expand"],
          icon: <PanelLeft {...iconProps} />,
          label: "Toggle sidebar",
          shortcut: "[",
          onSelect: () => runCommand(toggleSidebar),
          attrs: { "data-palette-command": "toggle-sidebar" },
        },
        ...THEME_COMMANDS.map((option): PaletteRow => {
          const Icon = option.icon;
          return {
            value: `command:theme-${option.value}`,
            keywords: [`Theme: ${option.label}`, "appearance", "mode"],
            icon: <Icon {...iconProps} />,
            label: `Theme: ${option.label}`,
            detail: theme === option.value ? <Check className="size-icon shrink-0 text-foreground" strokeWidth={ICON_STROKE_WIDTH} aria-label="Current theme" /> : undefined,
            onSelect: () => runCommand(() => setTheme(option.value)),
            attrs: { "data-palette-command": `theme-${option.value}` },
          };
        }),
        {
          value: "command:new-agent",
          keywords: ["New agent", "create"],
          icon: <Plus {...iconProps} />,
          label: "New agent",
          onSelect: () => go(routes.agentNew()),
          attrs: { "data-palette-command": "new-agent" },
        },
        {
          value: "command:settings",
          keywords: ["Go to settings", "preferences"],
          icon: <Settings {...iconProps} />,
          label: "Go to settings",
          shortcut: settingsHint,
          onSelect: () => go(routes.settings()),
          attrs: { "data-palette-command": "settings" },
        },
        {
          value: "command:shortcuts",
          keywords: ["Keyboard shortcuts", "help", "keys"],
          icon: <Keyboard {...iconProps} />,
          label: "Keyboard shortcuts",
          shortcut: "?",
          onSelect: openHelp,
          attrs: { "data-palette-command": "shortcuts" },
        },
      ],
    });
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent showClose={false} data-testid="command-palette" className="top-[12vh] max-w-xl translate-y-0 gap-0 overflow-hidden p-0">
        <VisuallyHidden>
          <DialogTitle>Command palette</DialogTitle>
          <DialogDescription>Search for a page, an agent, a workflow, a tool, an MCP server or a command.</DialogDescription>
        </VisuallyHidden>
        <Command loop vimBindings={false} filter={keywordFilter} label="Command palette" className="bg-transparent">
          <CommandInput
            value={query}
            onValueChange={setQuery}
            placeholder="Search pages, agents, workflows, tools…"
            aria-label="Search"
            data-testid="command-palette-input"
          />
          {/* Enter, Up, Down, Home and End on a scope must not reach cmdk (they would select or move
              the result). Everything else passes: Tab for the focus trap, Escape, the palette chord. */}
          <div
            data-testid="command-palette-scopes"
            className="min-w-0 overflow-x-auto border-b border-border px-2 py-1.5"
            onKeyDown={(event) => {
              if (CMDK_KEYS.has(event.key)) event.stopPropagation();
            }}
          >
            <SegmentedControl label="Scope" size="sm" options={PALETTE_SCOPES.map((option) => ({ value: option.value, label: option.label }))} value={scope} onValueChange={setScope} />
          </div>
          <CommandList>
            {/* cmdk's Empty, without the kit's live region: the announcer below is the only one. */}
            <CommandPrimitive.Empty className="px-3 py-6 text-center text-label text-muted-foreground">No matching results.</CommandPrimitive.Empty>
            {searching ? (
              // While searching, one group: cmdk sorts the items of a group by score, but does not
              // reorder groups, so a strong agent match would otherwise sit below a weak page match.
              <CommandGroup heading="Results">{sections.flatMap((section) => section.rows).map(renderRow)}</CommandGroup>
            ) : (
              sections.map((section) => (
                <CommandGroup key={section.id} heading={section.title}>
                  {section.rows.map(renderRow)}
                </CommandGroup>
              ))
            )}
          </CommandList>
          {loading ? (
            <p data-testid="command-palette-loading" className="px-3 py-2 text-caption text-muted-foreground">
              Loading…
            </p>
          ) : null}
          {failures.map((source) => {
            const state = sources[source.kind];
            return (
              <p key={source.kind} data-testid={source.testId} className="px-3 py-2 text-caption text-muted-foreground">
                Couldn&apos;t load {source.noun}
                {state?.status === "error" ? `: ${state.message}` : null}
              </p>
            );
          })}
          {capped.map((cap) => (
            <p key={cap.kind} data-testid="command-palette-more" data-kind={cap.kind} className="px-3 py-2 text-caption text-muted-foreground">
              Showing {PALETTE_ENTITY_LIMIT} of {cap.total} {cap.noun}. Type to search the rest.
            </p>
          ))}
          <PaletteAnnouncer loading={loading} failures={failures.map((source) => source.noun)} />
        </Command>
      </DialogContent>
    </Dialog>
  );
}
