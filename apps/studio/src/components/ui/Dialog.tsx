"use client";

import React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { IconButton } from "./IconButton";
import { useFocusReturn } from "./focusReturn";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;
export const DialogPortal = DialogPrimitive.Portal;

export const overlayClass = "fixed inset-0 z-50 bg-background/80";

export const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(function DialogOverlay({ className, ...props }, ref) {
  return <DialogPrimitive.Overlay ref={ref} className={cn(overlayClass, className)} {...props} />;
});

export interface DialogContentProps extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  /** Show the close (X) button in the corner. Default true. */
  showClose?: boolean;
  /** Portal target; defaults to <body>. */
  container?: HTMLElement | null;
}

/**
 * A centred modal on `surface-raised`: focus is trapped inside, Escape closes the top dialog only,
 * and focus returns to whatever opened it. Needs a `DialogTitle` (or a VisuallyHidden one).
 */
export const DialogContent = React.forwardRef<React.ElementRef<typeof DialogPrimitive.Content>, DialogContentProps>(function DialogContent(
  { className, children, showClose = true, container, onOpenAutoFocus, onCloseAutoFocus, ...props },
  ref,
) {
  const focus = useFocusReturn({ onOpenAutoFocus, onCloseAutoFocus });
  return (
    <DialogPrimitive.Portal container={container}>
      <DialogOverlay />
      <DialogPrimitive.Content
        ref={ref}
        onOpenAutoFocus={focus.onOpenAutoFocus}
        onCloseAutoFocus={focus.onCloseAutoFocus}
        className={cn(
          "fixed inset-x-4 top-1/2 z-50 mx-auto flex max-h-[calc(100dvh-2rem)] max-w-md -translate-y-1/2 flex-col gap-4 overflow-y-auto rounded-xl border border-border bg-surface-raised p-5 text-body text-foreground shadow-overlay outline-none",
          className,
        )}
        {...props}
      >
        {children}
        {showClose ? (
          <DialogPrimitive.Close asChild>
            <IconButton label="Close" size="sm" tooltip={false} className="absolute right-3 top-3" icon={<X strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />} />
          </DialogPrimitive.Close>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
});

export function DialogHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1 pr-8", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-wrap justify-end gap-2", className)} {...props} />;
}

export const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(function DialogTitle({ className, ...props }, ref) {
  return <DialogPrimitive.Title ref={ref} className={cn("text-title font-semibold text-foreground", className)} {...props} />;
});

export const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(function DialogDescription({ className, ...props }, ref) {
  return <DialogPrimitive.Description ref={ref} className={cn("text-body text-muted-foreground", className)} {...props} />;
});
