"use client";

import React, { useCallback, useContext, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { overlayClass } from "./Dialog";
import { IconButton } from "./IconButton";
import { useFocusReturn } from "./focusReturn";

/** At most this many Sheets are open at once; opening one more closes the bottom one. */
export const MAX_STACKED_SHEETS = 2;
/** How far each stacked Sheet sits from the right edge, so the one below stays visible. */
export const SHEET_STACK_OFFSET_PX = 24;

// The open Sheets, bottom to top. Module state, because stacked Sheets are often siblings in the
// React tree (a log Sheet opening a trace Sheet), not nested.
interface StackEntry {
  id: string;
  close: () => void;
  /** The open panel element. */
  element: () => HTMLElement | null;
  /** Where focus goes when this Sheet closes (shared with its useFocusReturn). */
  returnTarget: React.MutableRefObject<HTMLElement | null>;
  /** Set when the stack closed this Sheet: it then returns no focus. */
  autoClosed: React.MutableRefObject<boolean>;
}
let stack: StackEntry[] = [];
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function pushSheet(entry: StackEntry) {
  stack = [...stack, entry];
  emit();
  if (stack.length > MAX_STACKED_SHEETS) {
    const [bottom, above] = stack;
    // The Sheet above was usually opened from inside the bottom one, so its focus target is about
    // to disappear: it inherits the bottom Sheet's target instead (A's opener in an A → B → C chain).
    const aboveTarget = above.returnTarget.current;
    const bottomElement = bottom.element();
    if (!aboveTarget || (bottomElement && bottomElement.contains(aboveTarget))) above.returnTarget.current = bottom.returnTarget.current;
    bottom.autoClosed.current = true;
    bottom.close();
  }
}

function removeSheet(id: string) {
  const next = stack.filter((entry) => entry.id !== id);
  if (next.length === stack.length) return;
  stack = next;
  emit();
}

/** Ids of the open Sheets, bottom to top (for tests and the frame). */
export function openSheetIds(): string[] {
  return stack.map((entry) => entry.id);
}

function useSheetDepth(id: string): number {
  return useSyncExternalStore(
    subscribe,
    () => stack.findIndex((entry) => entry.id === id),
    () => -1,
  );
}

interface SheetContextValue {
  open: boolean;
  close: () => void;
}
const SheetContext = React.createContext<SheetContextValue | null>(null);

export interface SheetProps {
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  children: React.ReactNode;
}

/** A right-hand drawer (a modal Radix Dialog). Controlled with `open`/`onOpenChange`, or uncontrolled. */
export function Sheet({ open: openProp, defaultOpen = false, onOpenChange, children }: SheetProps) {
  const [uncontrolled, setUncontrolled] = useState(defaultOpen);
  const controlled = openProp !== undefined;
  const open = controlled ? openProp : uncontrolled;
  const setOpen = useCallback(
    (next: boolean) => {
      if (!controlled) setUncontrolled(next);
      onOpenChange?.(next);
    },
    [controlled, onOpenChange],
  );
  const context = useMemo(() => ({ open, close: () => setOpen(false) }), [open, setOpen]);
  return (
    <SheetContext.Provider value={context}>
      <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
        {children}
      </DialogPrimitive.Root>
    </SheetContext.Provider>
  );
}

export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetClose = DialogPrimitive.Close;

export type SheetSize = "md" | "wide" | "full";

const sizeClass: Record<SheetSize, string> = {
  // Full width on phones; 480 px, 60vw or the whole viewport from 768 px.
  md: "w-full md:w-[480px]",
  wide: "w-full md:w-[60vw]",
  full: "w-full",
};

export interface SheetContentProps extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  /** md 480 px, wide 60vw, full. Below 768 px every size is full width. */
  size?: SheetSize;
  /** Portal target; defaults to <body>. */
  container?: HTMLElement | null;
}

/**
 * The drawer panel. Focus is trapped while open and returns to whatever opened it; Escape and the
 * overlay close only the top Sheet. A second Sheet stacks over the first, offset 24 px from the right;
 * a third closes the bottom one, which hands its focus target to the Sheet above.
 */
export const SheetContent = React.forwardRef<React.ElementRef<typeof DialogPrimitive.Content>, SheetContentProps>(function SheetContent(
  { size = "md", container, className, style, children, onOpenAutoFocus, onCloseAutoFocus, ...props },
  ref,
) {
  const id = useId();
  const sheet = useContext(SheetContext);
  const depth = useSheetDepth(id);
  const autoClosed = useRef(false);
  const skipReturn = useCallback(() => autoClosed.current, []);
  const focus = useFocusReturn({ onOpenAutoFocus, onCloseAutoFocus }, skipReturn);
  const elementRef = useRef<HTMLDivElement | null>(null);
  const setRefs = useCallback(
    (node: HTMLDivElement | null) => {
      elementRef.current = node;
      if (typeof ref === "function") ref(node);
      else if (ref) ref.current = node;
    },
    [ref],
  );

  // Register while open (Radix renders the panel only then; this wrapper renders either way). The
  // latest close is read through a ref so a re-render (an inline onOpenChange) never re-registers
  // and reorders the stack. Without a <Sheet> parent the panel counts as open.
  const isOpen = sheet?.open ?? true;
  const closeRef = useRef(sheet?.close);
  closeRef.current = sheet?.close;
  useLayoutEffect(() => {
    if (!isOpen) return;
    autoClosed.current = false;
    pushSheet({ id, close: () => closeRef.current?.(), element: () => elementRef.current, returnTarget: focus.target, autoClosed });
    return () => removeSheet(id);
  }, [id, isOpen, focus.target]);

  const offset = Math.max(depth, 0) * SHEET_STACK_OFFSET_PX;
  return (
    <DialogPrimitive.Portal container={container}>
      <DialogPrimitive.Overlay className={overlayClass} data-sheet-overlay="" />
      <DialogPrimitive.Content
        ref={setRefs}
        data-sheet=""
        data-sheet-size={size}
        data-sheet-depth={Math.max(depth, 0)}
        onOpenAutoFocus={focus.onOpenAutoFocus}
        onCloseAutoFocus={focus.onCloseAutoFocus}
        style={{ ...style, ...(offset ? { right: offset, maxWidth: `calc(100% - ${offset}px)` } : null) }}
        className={cn(
          "fixed inset-y-0 right-0 z-50 flex h-dvh min-w-0 flex-col border-l border-border bg-surface-raised text-body text-foreground shadow-overlay outline-none",
          sizeClass[size],
          className,
        )}
        {...props}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
});

export const SheetTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(function SheetTitle({ className, ...props }, ref) {
  return <DialogPrimitive.Title ref={ref} className={cn("min-w-0 truncate text-title font-semibold text-foreground", className)} {...props} />;
});

export const SheetDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(function SheetDescription({ className, ...props }, ref) {
  return <DialogPrimitive.Description ref={ref} className={cn("text-label text-muted-foreground", className)} {...props} />;
});

export interface SheetHeaderProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  /** Rendered as the Sheet's accessible title. */
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Buttons before the close button (prev / next, expand, copy id). */
  actions?: React.ReactNode;
  /** Show the close button. Default true. */
  showClose?: boolean;
}

/** The Sheet's 44 px header: title, optional description, actions and a Close button. */
export function SheetHeader({ title, description, actions, showClose = true, className, children, ...props }: SheetHeaderProps) {
  return (
    <div className={cn("flex min-h-header shrink-0 items-center gap-2 border-b border-border px-4 py-2", className)} {...props}>
      <div className="flex min-w-0 flex-1 flex-col">
        <SheetTitle>{title}</SheetTitle>
        {description ? <SheetDescription>{description}</SheetDescription> : null}
      </div>
      {children}
      {actions ? <div className="flex shrink-0 items-center gap-1">{actions}</div> : null}
      {/* No tooltip: focus lands here when the Sheet opens, and an open tooltip would take the first Escape. */}
      {showClose ? (
        <DialogPrimitive.Close asChild>
          <IconButton label="Close" size="sm" tooltip={false} icon={<X strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />} />
        </DialogPrimitive.Close>
      ) : null}
    </div>
  );
}

/** The scrolling body between header and footer. */
export function SheetBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("min-h-0 min-w-0 flex-1 overflow-y-auto p-4", className)} {...props} />;
}

export function SheetFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex shrink-0 flex-wrap justify-end gap-2 border-t border-border px-4 py-3", className)} {...props} />;
}
