"use client";

import React, { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { IconButton } from "@/components/ui/IconButton";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import {
  clampSidePanelWidth,
  readSidePanelWidth,
  SIDE_PANEL_DEFAULT_WIDTH,
  SIDE_PANEL_MIN_WIDTH,
  sidePanelMaxWidth,
  writeSidePanelWidth,
} from "./frameStorage";
import { matchesNow, useIsWide, WIDE_QUERY } from "./useMediaQuery";

/** Keyboard resize step, in px. */
export const SIDE_PANEL_KEY_STEP = 16;

interface Registration {
  id: string;
  title: string;
}

interface SidePanelContextValue {
  active: Registration | null;
  register: (registration: Registration, defaultOpen: boolean) => () => void;
  rename: (id: string, title: string) => void;
  open: boolean;
  setOpen: (open: boolean) => void;
  /** The saved width before clamping to the window. */
  width: number;
  setWidth: (width: number, persist: boolean) => void;
  /** The element the open panel's content portals into (wide screens). */
  slot: HTMLElement | null;
  setSlot: (element: HTMLElement | null) => void;
  isWide: boolean;
}

const SidePanelContext = createContext<SidePanelContextValue | null>(null);

/** Holds the route side panel: one registered panel at a time, its open state and its width. */
export function SidePanelProvider({ children }: { children: React.ReactNode }) {
  const [active, setActive] = useState<Registration | null>(null);
  const [open, setOpen] = useState(false);
  const [width, setWidthState] = useState(SIDE_PANEL_DEFAULT_WIDTH);
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const isWide = useIsWide();

  useEffect(() => {
    setWidthState(readSidePanelWidth());
  }, []);

  // The registration, also in a ref so cleanup and rename compare against the latest one.
  const activeRef = useRef<Registration | null>(null);

  const register = useCallback((registration: Registration, defaultOpen: boolean) => {
    activeRef.current = registration;
    setActive(registration);
    // `defaultOpen` means "open as a column". Below 1024 px the panel would be a modal Sheet over
    // the page, so it starts closed there. Read the media query now: during hydration `isWide` is
    // still the server's guess (wide).
    setOpen(defaultOpen && matchesNow(WIDE_QUERY));
    return () => {
      if (activeRef.current?.id !== registration.id) return;
      activeRef.current = null;
      setActive(null);
      setOpen(false);
    };
  }, []);

  const rename = useCallback((id: string, title: string) => {
    const current = activeRef.current;
    if (!current || current.id !== id || current.title === title) return;
    activeRef.current = { id, title };
    setActive(activeRef.current);
  }, []);

  const setWidth = useCallback((next: number, persist: boolean) => {
    setWidthState(next);
    if (persist) writeSidePanelWidth(next);
  }, []);

  // Narrowing the window closes an open column instead of turning it into a modal Sheet.
  useEffect(() => {
    if (!isWide) setOpen(false);
  }, [isWide]);

  const value = useMemo<SidePanelContextValue>(
    () => ({ active, register, rename, open, setOpen, width, setWidth, slot, setSlot, isWide }),
    [active, register, rename, open, width, setWidth, slot, isWide],
  );
  return <SidePanelContext.Provider value={value}>{children}</SidePanelContext.Provider>;
}

export interface SidePanelControls {
  /** A page has registered a panel. */
  available: boolean;
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
  /** The registered panel's title. */
  title: string | null;
}

const noop = () => {};

/** Open, close or toggle the route side panel (a page's "Config" button). Inert outside the frame. */
export function useSidePanel(): SidePanelControls {
  const context = useContext(SidePanelContext);
  const setOpen = context?.setOpen;
  const open = context?.open ?? false;
  const toggle = useCallback(() => setOpen?.(!open), [open, setOpen]);
  if (!context) return { available: false, open: false, setOpen: noop, toggle: noop, title: null };
  return { available: context.active !== null, open: context.active !== null && context.open, setOpen: context.setOpen, toggle, title: context.active?.title ?? null };
}

export interface SidePanelProps {
  /** Accessible name and header text ("Config"). */
  title: string;
  /** Open when the page mounts. Default false: the panel is collapsed except where a page asks. */
  defaultOpen?: boolean;
  children: React.ReactNode;
}

/**
 * Registers the page's right-hand panel with the frame. From 1024 px it renders in the frame's slot
 * beside the main card (resizable, 360 px to half the window); below that it is a Sheet from the
 * right, so it never takes a column on a phone. Outside the frame (a page rendered alone in a test)
 * the content renders in place.
 */
export function SidePanel({ title, defaultOpen = false, children }: SidePanelProps) {
  const context = useContext(SidePanelContext);
  const id = useId();
  const register = context?.register;
  const rename = context?.rename;
  // Registered once per mount; the title is kept current separately so a rename does not reset `open`.
  const defaultOpenRef = useRef(defaultOpen);
  const titleRef = useRef(title);
  useLayoutEffect(() => {
    if (!register) return;
    return register({ id, title: titleRef.current }, defaultOpenRef.current);
  }, [id, register]);
  useLayoutEffect(() => {
    titleRef.current = title;
    rename?.(id, title);
  }, [id, rename, title]);

  if (!context) {
    return (
      <aside aria-label={title} data-testid="side-panel" className="min-w-0">
        {children}
      </aside>
    );
  }
  if (context.active?.id !== id) return null;
  if (context.isWide) return context.open && context.slot ? createPortal(children, context.slot) : null;
  return (
    <Sheet open={context.open} onOpenChange={context.setOpen}>
      <SheetContent size="md" aria-describedby={undefined} data-testid="side-panel-sheet">
        <SheetHeader title={title} />
        <SheetBody>{children}</SheetBody>
      </SheetContent>
    </Sheet>
  );
}

// The window width, for clamping the panel to half of it.
function subscribeResize(onChange: () => void) {
  window.addEventListener("resize", onChange);
  return () => window.removeEventListener("resize", onChange);
}
const windowWidth = () => window.innerWidth;
const serverWindowWidth = () => 1280;

/**
 * The frame's column for the open side panel (wide screens only): a resize handle, a header with
 * the title and Close, and the body the panel portals into. Renders nothing when no panel is open.
 */
export function SidePanelSlot({ className }: { className?: string }) {
  const context = useContext(SidePanelContext);
  const shown = Boolean(context && context.isWide && context.active && context.open);
  // Listen to window resizes only while a panel is shown.
  const subscribe = useCallback((onChange: () => void) => (shown ? subscribeResize(onChange) : () => {}), [shown]);
  const viewport = useSyncExternalStore(subscribe, windowWidth, serverWindowWidth);
  const titleId = useId();
  if (!context || !context.active || !shown) return null;
  const width = clampSidePanelWidth(context.width, viewport);
  return (
    <>
      <SidePanelResizeHandle width={width} viewport={viewport} onResize={context.setWidth} />
      <aside
        aria-labelledby={titleId}
        data-testid="side-panel"
        style={{ width }}
        className={cn("flex min-h-0 shrink-0 flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-raised", className)}
      >
        <div className="flex min-h-header shrink-0 items-center gap-2 border-b border-border px-3">
          <h2 id={titleId} className="min-w-0 flex-1 truncate text-label font-semibold text-foreground">
            {context.active.title}
          </h2>
          <IconButton
            label={`Close ${context.active.title}`}
            size="sm"
            tooltipSide="left"
            onClick={() => context.setOpen(false)}
            icon={<X strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
            data-testid="side-panel-close"
          />
        </div>
        <div ref={context.setSlot} data-testid="side-panel-body" className="min-h-0 min-w-0 flex-1 overflow-y-auto" />
      </aside>
    </>
  );
}

interface HandleProps {
  width: number;
  viewport: number;
  onResize: (width: number, persist: boolean) => void;
}

/** A keyboard and pointer resize handle on the panel's left edge (ArrowLeft widens, ArrowRight narrows). */
function SidePanelResizeHandle({ width, viewport, onResize }: HandleProps) {
  const drag = useRef<{ startX: number; startWidth: number; last: number } | null>(null);
  const max = sidePanelMaxWidth(viewport);

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    let next: number | null = null;
    if (event.key === "ArrowLeft") next = width + SIDE_PANEL_KEY_STEP;
    else if (event.key === "ArrowRight") next = width - SIDE_PANEL_KEY_STEP;
    else if (event.key === "Home") next = SIDE_PANEL_MIN_WIDTH;
    else if (event.key === "End") next = max;
    if (next === null) return;
    event.preventDefault();
    onResize(clampSidePanelWidth(next, viewport), true);
  }

  function onPointerDown(event: React.PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    drag.current = { startX: event.clientX, startWidth: width, last: width };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function onPointerMove(event: React.PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    // The panel is on the right: moving the pointer left widens it.
    const next = clampSidePanelWidth(drag.current.startWidth + (drag.current.startX - event.clientX), viewport);
    drag.current.last = next;
    onResize(next, false);
  }

  function onPointerUp(event: React.PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    onResize(drag.current.last, true);
    drag.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize side panel"
      aria-valuemin={SIDE_PANEL_MIN_WIDTH}
      aria-valuemax={max}
      aria-valuenow={width}
      tabIndex={0}
      data-testid="side-panel-resize"
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      className="group relative -mx-1 flex w-2 shrink-0 cursor-col-resize touch-none items-stretch justify-center outline-none"
    >
      <span className="w-px rounded-full bg-transparent transition-colors group-hover:bg-border-strong group-focus-visible:bg-ring motion-reduce:transition-none" />
    </div>
  );
}
