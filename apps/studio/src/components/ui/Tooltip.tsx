"use client";

import React from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { Slot } from "@radix-ui/react-slot";
import { cn } from "@/lib/cn";
import { Kbd } from "./Kbd";

/**
 * Mount exactly once, near the root (the app frame does). Every `Tooltip` needs it: Radix throws
 * without one. Tests that render tooltips wrap the tree in it (`render(ui, { wrapper: TooltipProvider })`).
 */
export function TooltipProvider({ delayDuration = 300, ...props }: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return <TooltipPrimitive.Provider delayDuration={delayDuration} {...props} />;
}

export const TooltipRoot = TooltipPrimitive.Root;
export const TooltipTrigger = TooltipPrimitive.Trigger;

export const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(function TooltipContent({ className, sideOffset = 6, ...props }, ref) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        ref={ref}
        sideOffset={sideOffset}
        className={cn(
          "z-50 flex max-w-xs items-center gap-2 rounded-md border border-border bg-surface-raised px-2 py-1 text-caption text-foreground shadow-raised",
          className,
        )}
        {...props}
      />
    </TooltipPrimitive.Portal>
  );
});

export interface TooltipProps {
  /** The tooltip text. */
  content: React.ReactNode;
  /** A shortcut shown after the text, as a `<Kbd>` ("⌘K", "[" ). */
  shortcut?: string;
  side?: React.ComponentProps<typeof TooltipPrimitive.Content>["side"];
  align?: React.ComponentProps<typeof TooltipPrimitive.Content>["align"];
  /** Hide the tooltip (for example when a sidebar label is already visible). */
  disabled?: boolean;
  /** One focusable element; it keeps its own accessible name (the tooltip is a description). */
  children: React.ReactElement;
}

/** The trigger's own accessible name, when it is a plain `aria-label` or text child. */
function triggerName(child: React.ReactElement): string | null {
  const props = child.props as { "aria-label"?: unknown; children?: unknown };
  if (typeof props["aria-label"] === "string") return props["aria-label"];
  if (typeof props.children === "string") return props.children;
  return null;
}

/**
 * Sits between the Radix trigger and the real child, and drops the `aria-describedby` the trigger
 * adds (the tooltip's id). Everything else, handlers included, is merged onto the child by Slot,
 * which also keeps any description the child sets itself.
 */
const WithoutTooltipDescription = React.forwardRef<HTMLElement, React.HTMLAttributes<HTMLElement> & { children: React.ReactElement }>(
  function WithoutTooltipDescription(props, ref) {
    const rest: Record<string, unknown> = { ...props };
    delete rest["aria-describedby"];
    return <Slot ref={ref} {...rest} />;
  },
);

/**
 * A hover and focus tooltip around one trigger element. Escape closes it. Needs `TooltipProvider`
 * above it. When the text only repeats the trigger's name (an icon button's label), it is not
 * wired as a description, so screen readers do not read it twice.
 */
export function Tooltip({ content, shortcut, side = "top", align, disabled = false, children }: TooltipProps) {
  if (disabled) return children;
  const repeatsName = typeof content === "string" && content.trim() === triggerName(children)?.trim();
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>
        {repeatsName ? <WithoutTooltipDescription>{children}</WithoutTooltipDescription> : children}
      </TooltipPrimitive.Trigger>
      <TooltipContent side={side} align={align}>
        <span>{content}</span>
        {shortcut ? <Kbd>{shortcut}</Kbd> : null}
      </TooltipContent>
    </TooltipPrimitive.Root>
  );
}
