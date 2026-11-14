"use client";

import React, { useCallback, useLayoutEffect, useRef } from "react";
import { Command as CommandPrimitive, useCommandState } from "cmdk";
import { Search } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "./Dialog";
import { Kbd } from "./Kbd";
import { VisuallyHidden } from "./VisuallyHidden";

/** A filterable list with keyboard selection (the palette and Combobox are built on it). */
export const Command = React.forwardRef<React.ElementRef<typeof CommandPrimitive>, React.ComponentPropsWithoutRef<typeof CommandPrimitive>>(
  function Command({ className, ...props }, ref) {
    return <CommandPrimitive ref={ref} className={cn("flex h-full w-full min-w-0 flex-col overflow-hidden bg-surface-raised text-foreground", className)} {...props} />;
  },
);

export const CommandInput = React.forwardRef<React.ElementRef<typeof CommandPrimitive.Input>, React.ComponentPropsWithoutRef<typeof CommandPrimitive.Input>>(
  function CommandInput({ className, ...props }, ref) {
    return (
      <div className="flex items-center gap-2 border-b border-border px-3" cmdk-input-wrapper="">
        <Search className="size-icon shrink-0 text-muted-foreground" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        <CommandPrimitive.Input
          ref={ref}
          className={cn("h-10 w-full min-w-0 bg-transparent text-body text-foreground outline-none placeholder:text-muted-foreground disabled:opacity-50", className)}
          {...props}
        />
      </div>
    );
  },
);

export const CommandList = React.forwardRef<React.ElementRef<typeof CommandPrimitive.List>, React.ComponentPropsWithoutRef<typeof CommandPrimitive.List>>(
  function CommandList({ className, ...props }, ref) {
    const listRef = useRef<HTMLDivElement | null>(null);
    const empty = useCommandState((state) => state.filtered.count === 0);
    // cmdk always renders role="listbox" (set after our props), which leaves an empty listbox when
    // nothing matches (axe aria-required-children). While empty, the element becomes role="none";
    // its tabindex is dropped too, since a focusable element ignores role="none". React does not
    // touch either attribute again because their props never change, so both are restored here.
    useLayoutEffect(() => {
      const element = listRef.current;
      if (!element) return;
      if (empty) {
        element.setAttribute("role", "none");
        element.removeAttribute("tabindex");
      } else {
        element.setAttribute("role", "listbox");
        element.setAttribute("tabindex", "-1");
      }
    }, [empty]);
    const setRefs = useCallback(
      (node: HTMLDivElement | null) => {
        listRef.current = node;
        if (typeof ref === "function") ref(node);
        else if (ref) ref.current = node;
      },
      [ref],
    );
    return <CommandPrimitive.List ref={setRefs} className={cn("max-h-80 overflow-y-auto overflow-x-hidden p-1", className)} {...props} />;
  },
);

export const CommandEmpty = React.forwardRef<React.ElementRef<typeof CommandPrimitive.Empty>, React.ComponentPropsWithoutRef<typeof CommandPrimitive.Empty>>(
  function CommandEmpty({ className, children = "No matching results.", ...props }, ref) {
    return (
      <CommandPrimitive.Empty ref={ref} className={cn("px-3 py-6 text-center text-label text-muted-foreground", className)} {...props}>
        {/* cmdk marks the empty element role="presentation"; the inner status announces it. */}
        <div role="status" aria-live="polite">{children}</div>
      </CommandPrimitive.Empty>
    );
  },
);

export const CommandLoading = CommandPrimitive.Loading;

export const CommandGroup = React.forwardRef<React.ElementRef<typeof CommandPrimitive.Group>, React.ComponentPropsWithoutRef<typeof CommandPrimitive.Group>>(
  function CommandGroup({ className, ...props }, ref) {
    return (
      <CommandPrimitive.Group
        ref={ref}
        className={cn("overflow-hidden p-1 text-foreground [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1 [&_[cmdk-group-heading]]:text-caption [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-muted-foreground", className)}
        {...props}
      />
    );
  },
);

export const CommandSeparator = React.forwardRef<React.ElementRef<typeof CommandPrimitive.Separator>, React.ComponentPropsWithoutRef<typeof CommandPrimitive.Separator>>(
  function CommandSeparator({ className, ...props }, ref) {
    return <CommandPrimitive.Separator ref={ref} className={cn("-mx-1 h-px bg-border", className)} {...props} />;
  },
);

export const CommandItem = React.forwardRef<React.ElementRef<typeof CommandPrimitive.Item>, React.ComponentPropsWithoutRef<typeof CommandPrimitive.Item>>(
  function CommandItem({ className, ...props }, ref) {
    return (
      <CommandPrimitive.Item
        ref={ref}
        className={cn(
          "relative flex min-h-8 cursor-default select-none items-center gap-2 rounded-sm px-2 py-1 text-label outline-none",
          "data-[selected=true]:bg-fill-active data-[disabled=true]:pointer-events-none data-[disabled=true]:opacity-50 [&_svg]:size-icon [&_svg]:shrink-0",
          className,
        )}
        {...props}
      />
    );
  },
);

/** A shortcut hint at the end of a CommandItem. */
export function CommandShortcut({ className, ...props }: React.HTMLAttributes<HTMLElement>) {
  return <Kbd className={cn("ml-auto", className)} {...props} />;
}

export interface CommandDialogProps extends React.ComponentPropsWithoutRef<typeof Dialog> {
  /** Accessible title (visually hidden). */
  title?: string;
  /** Accessible description (visually hidden). */
  description?: string;
  /** Props for the inner Command (filter, value, shouldFilter, loop…). */
  commandProps?: React.ComponentPropsWithoutRef<typeof Command>;
  children: React.ReactNode;
}

/** A Command inside a modal Dialog, near the top of the screen (the ⌘K palette's shell). */
export function CommandDialog({ title = "Command palette", description = "Search for a page, an item or a command.", commandProps, children, ...props }: CommandDialogProps) {
  return (
    <Dialog {...props}>
      <DialogContent showClose={false} className="top-[15vh] max-w-xl translate-y-0 gap-0 overflow-hidden p-0">
        <VisuallyHidden>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </VisuallyHidden>
        <Command loop {...commandProps}>
          {children}
        </Command>
      </DialogContent>
    </Dialog>
  );
}
