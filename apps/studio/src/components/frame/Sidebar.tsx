"use client";

import React, { useId } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Monitor, Moon, PanelLeftClose, PanelLeftOpen, Search, Sun } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { activeNavItem, navSections, type NavItem, type NavSection } from "@/lib/nav";
import type { ThemePreference } from "@/lib/theme";
import { focusRing } from "@/components/ui/Button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/DropdownMenu";
import { IconButton } from "@/components/ui/IconButton";
import { Kbd } from "@/components/ui/Kbd";
import { usePaletteChordLabel, usePaletteKeyShortcuts } from "@/components/shortcuts/usePaletteChordLabel";
import { Tooltip } from "@/components/ui/Tooltip";
import { useTheme } from "@/components/theme/ThemeProvider";
import { PRODUCT_NAME } from "./brand";
import { useFrame } from "./FrameContext";

export const SIDEBAR_ID = "studio-sidebar";

/** `NEXT_PUBLIC_STUDIO_VERSION`, or null when unset (then no version line is shown). */
export function studioVersion(): string | null {
  const version = process.env.NEXT_PUBLIC_STUDIO_VERSION;
  return version && version.trim() ? version.trim() : null;
}

/** The product's plain monogram: two letters on an inverse tile, no logo. */
export function Monogram({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-testid="studio-monogram"
      className={cn("grid size-7 shrink-0 place-items-center rounded-md bg-primary text-badge font-semibold tracking-tight text-primary-foreground", className)}
    >
      AI
    </span>
  );
}

interface NavLinkProps {
  item: NavItem;
  active: boolean;
  /** Icon rail: the label becomes the tooltip and the accessible name. */
  rail: boolean;
  onNavigate?: () => void;
}

function NavLink({ item, active, rail, onNavigate }: NavLinkProps) {
  const Icon = item.icon;
  const link = (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      aria-label={rail ? item.label : undefined}
      data-nav-id={item.id}
      data-rail-item=""
      onClick={onNavigate}
      className={cn(
        "flex h-sidebar-item min-w-0 items-center gap-2 rounded-md px-2 text-label font-medium text-muted-foreground transition-colors motion-reduce:transition-none",
        "hover:bg-fill-hover hover:text-foreground",
        active && "bg-fill-selected text-foreground",
        rail && "w-sidebar-item justify-center px-0",
        focusRing,
      )}
    >
      <Icon className="size-icon shrink-0" aria-hidden="true" />
      {rail ? null : <span data-rail-hidden="" className="min-w-0 truncate">{item.label}</span>}
    </Link>
  );
  return (
    <Tooltip content={item.label} side="right" disabled={!rail}>
      {link}
    </Tooltip>
  );
}

interface NavGroupListProps {
  section: NavSection;
  activeId: string | null;
  rail: boolean;
  onNavigate?: () => void;
}

function NavGroupList({ section, activeId, rail, onNavigate }: NavGroupListProps) {
  const labelId = useId();
  const title = section.group.title;
  return (
    <div data-nav-group={section.group.id} className="flex flex-col gap-0.5">
      {title ? (
        <span id={labelId} data-rail-hidden="" className={cn("px-2 pb-1 pt-2 text-caption font-medium text-muted-foreground", rail && "sr-only")}>
          {title}
        </span>
      ) : null}
      <ul aria-labelledby={title ? labelId : undefined} className={cn("flex flex-col gap-0.5", rail && "items-center")}>
        {section.items.map((item) => (
          <li key={item.id} className={cn("min-w-0", !rail && "w-full")}>
            <NavLink item={item} active={item.id === activeId} rail={rail} onNavigate={onNavigate} />
          </li>
        ))}
      </ul>
    </div>
  );
}

const THEME_OPTIONS: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: "system", label: "System", icon: Monitor },
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
];

/**
 * The footer theme menu. The trigger's sun and moon switch with the `dark:` class on <html> (set by
 * the head script before paint), not with the resolved theme, so the icon never flickers on load.
 */
export function ThemeMenu({ rail }: { rail: boolean }) {
  const { theme, setTheme } = useTheme();
  const current = THEME_OPTIONS.find((option) => option.value === theme) ?? THEME_OPTIONS[0];
  const icons = (
    <>
      <Sun className="size-icon shrink-0 dark:hidden" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
      <Moon className="hidden size-icon shrink-0 dark:block" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
    </>
  );
  return (
    <DropdownMenu>
      {rail ? (
        <DropdownMenuTrigger asChild>
          <IconButton label={`Theme: ${current.label}`} tooltipSide="right" icon={icons} data-testid="theme-menu-trigger" />
        </DropdownMenuTrigger>
      ) : (
        <DropdownMenuTrigger
          aria-label={`Theme: ${current.label}`}
          data-testid="theme-menu-trigger"
          data-rail-item=""
          className={cn(
            "flex h-sidebar-item w-full min-w-0 items-center gap-2 rounded-md px-2 text-label font-medium text-muted-foreground hover:bg-fill-hover hover:text-foreground",
            focusRing,
          )}
        >
          {icons}
          <span data-rail-hidden="" className="min-w-0 truncate">Theme</span>
          <span data-rail-hidden="" className="ml-auto text-caption">{current.label}</span>
        </DropdownMenuTrigger>
      )}
      <DropdownMenuContent side={rail ? "right" : "top"} align="start" className="min-w-40">
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme} onValueChange={(value) => setTheme(value as ThemePreference)}>
          {THEME_OPTIONS.map((option) => {
            const Icon = option.icon;
            return (
              <DropdownMenuRadioItem key={option.value} value={option.value} data-testid={`theme-option-${option.value}`}>
                <Icon className="size-icon" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                {option.label}
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export interface SidebarNavProps {
  /** Icon rail (desktop collapsed). */
  rail?: boolean;
  /** Called after a link is followed (the drawer closes). */
  onNavigate?: () => void;
  /** Accessible name of the nav landmark. */
  label?: string;
}

/** The grouped nav list plus the footer (Settings, theme, version). Shared by the sidebar and the drawer. */
export function SidebarNav({ rail = false, onNavigate, label = "Main" }: SidebarNavProps) {
  const pathname = usePathname();
  const activeId = activeNavItem(pathname)?.id ?? null;
  const main = navSections.filter((section) => section.group.placement === "main");
  const footer = navSections.filter((section) => section.group.placement === "footer");
  const version = studioVersion();
  return (
    <>
      <nav aria-label={label} className={cn("flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-2 py-2", rail && "items-center")}>
        {main.map((section, index) => (
          <React.Fragment key={section.group.id}>
            {rail && index > 0 ? <span aria-hidden="true" className="h-px w-6 shrink-0 bg-border" /> : null}
            <NavGroupList section={section} activeId={activeId} rail={rail} onNavigate={onNavigate} />
          </React.Fragment>
        ))}
      </nav>
      <div data-testid="sidebar-footer" className={cn("flex shrink-0 flex-col gap-0.5 border-t border-border px-2 py-2", rail && "items-center")}>
        {footer.length > 0 ? (
          <nav aria-label={`${label} footer`} className={cn("flex flex-col gap-0.5", rail && "items-center")}>
            {footer.map((section) => (
              <NavGroupList key={section.group.id} section={section} activeId={activeId} rail={rail} onNavigate={onNavigate} />
            ))}
          </nav>
        ) : null}
        <ThemeMenu rail={rail} />
        {version && !rail ? (
          <p data-testid="studio-version" data-rail-hidden="" className="truncate px-2 pt-1 text-caption text-muted-foreground">
            Version {version}
          </p>
        ) : null}
      </div>
    </>
  );
}

/**
 * The desktop sidebar (1024 px and up): 240 px, or a 56 px icon rail when collapsed with `[` or the
 * toggle. Hidden by CSS below 1024 px, where the mobile top bar and drawer take over.
 */
export function Sidebar() {
  const frame = useFrame();
  const collapsed = frame?.sidebarCollapsed ?? false;
  return (
    <aside
      id={SIDEBAR_ID}
      aria-label="Sidebar"
      data-testid="sidebar"
      data-collapsed={collapsed ? "true" : "false"}
      data-sidebar-root=""
      className={cn(
        "hidden h-full shrink-0 flex-col bg-canvas transition-[width] duration-150 motion-reduce:transition-none lg:flex",
        collapsed ? "w-14" : "w-60",
      )}
    >
      <div data-sidebar-head="" className={cn("flex h-header shrink-0 items-center gap-2 px-3", collapsed && "flex-col justify-center px-0")}>
        {collapsed ? null : (
          <>
            <Monogram />
            <span data-rail-hidden="" className="min-w-0 flex-1 truncate text-label font-semibold text-foreground">{PRODUCT_NAME}</span>
          </>
        )}
        <SidebarToggle />
      </div>
      <SidebarSearch rail={collapsed} />
      <SidebarNav rail={collapsed} />
    </aside>
  );
}

/** "Search… ⌘K" ("Ctrl K" off macOS): opens the command palette. Shown once a palette has registered (SL.5). */
export function SidebarSearch({ rail }: { rail: boolean }) {
  const frame = useFrame();
  const chord = usePaletteChordLabel();
  const keyShortcuts = usePaletteKeyShortcuts();
  const openPalette = frame?.openPalette;
  if (!openPalette) return null;
  const icon = <Search className="size-icon shrink-0" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />;
  if (rail) {
    return (
      <div className="flex shrink-0 justify-center px-2 pb-1">
        <IconButton label="Search" shortcut={chord} aria-keyshortcuts={keyShortcuts} tooltipSide="right" onClick={openPalette} data-testid="sidebar-search" icon={icon} />
      </div>
    );
  }
  return (
    <div className="shrink-0 px-2 pb-1">
      <button
        type="button"
        onClick={openPalette}
        aria-keyshortcuts={keyShortcuts}
        data-testid="sidebar-search"
        className={cn(
          "flex h-sidebar-item w-full min-w-0 items-center gap-2 rounded-md border border-border bg-surface px-2 text-label text-muted-foreground hover:bg-fill-hover hover:text-foreground",
          focusRing,
        )}
      >
        {icon}
        <span className="min-w-0 flex-1 truncate text-left">Search…</span>
        <Kbd>{chord}</Kbd>
      </button>
    </div>
  );
}

/** Collapse or expand the sidebar. Its tooltip shows `[`, so it also says so to assistive tech. */
export function SidebarToggle() {
  const frame = useFrame();
  if (!frame) return null;
  const collapsed = frame.sidebarCollapsed;
  const Icon = collapsed ? PanelLeftOpen : PanelLeftClose;
  return (
    <IconButton
      label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
      shortcut="["
      aria-keyshortcuts="["
      aria-expanded={!collapsed}
      aria-controls={SIDEBAR_ID}
      tooltipSide="right"
      size="sm"
      onClick={frame.toggleSidebar}
      data-testid="sidebar-toggle"
      icon={<Icon strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
    />
  );
}
