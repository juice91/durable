"use client";

import React from "react";
import * as MenuPrimitive from "@radix-ui/react-dropdown-menu";
import { Check, ChevronRight, Circle } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Kbd } from "./Kbd";

export const DropdownMenu = MenuPrimitive.Root;
export const DropdownMenuTrigger = MenuPrimitive.Trigger;
export const DropdownMenuGroup = MenuPrimitive.Group;
export const DropdownMenuSub = MenuPrimitive.Sub;
export const DropdownMenuRadioGroup = MenuPrimitive.RadioGroup;

const menuSurface = "z-50 min-w-40 max-w-[var(--radix-dropdown-menu-content-available-width)] overflow-hidden rounded-lg border border-border bg-surface-raised p-1 text-label text-foreground shadow-raised";
const itemBase =
  "relative flex min-h-7 cursor-default select-none items-center gap-2 rounded-sm px-2 py-1 outline-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 data-[highlighted]:bg-fill-active [&_svg]:size-icon [&_svg]:shrink-0";

export const DropdownMenuContent = React.forwardRef<
  React.ElementRef<typeof MenuPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof MenuPrimitive.Content>
>(function DropdownMenuContent({ className, sideOffset = 6, align = "start", collisionPadding = 8, ...props }, ref) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Content ref={ref} sideOffset={sideOffset} align={align} collisionPadding={collisionPadding} className={cn(menuSurface, className)} {...props} />
    </MenuPrimitive.Portal>
  );
});

export interface DropdownMenuItemProps extends React.ComponentPropsWithoutRef<typeof MenuPrimitive.Item> {
  /** Red text for delete-like actions. */
  destructive?: boolean;
  /** Shortcut hint shown at the end of the row. */
  shortcut?: string;
}

export const DropdownMenuItem = React.forwardRef<React.ElementRef<typeof MenuPrimitive.Item>, DropdownMenuItemProps>(function DropdownMenuItem(
  { className, destructive = false, shortcut, children, ...props },
  ref,
) {
  return (
    <MenuPrimitive.Item ref={ref} className={cn(itemBase, destructive && "text-destructive-text", className)} {...props}>
      {/* asChild (a link item) slots onto exactly one child, so no shortcut hint there. */}
      {props.asChild ? (
        children
      ) : (
        <>
          {children}
          {shortcut ? <Kbd className="ml-auto">{shortcut}</Kbd> : null}
        </>
      )}
    </MenuPrimitive.Item>
  );
});

export const DropdownMenuCheckboxItem = React.forwardRef<
  React.ElementRef<typeof MenuPrimitive.CheckboxItem>,
  React.ComponentPropsWithoutRef<typeof MenuPrimitive.CheckboxItem>
>(function DropdownMenuCheckboxItem({ className, children, ...props }, ref) {
  return (
    <MenuPrimitive.CheckboxItem ref={ref} className={cn(itemBase, "pl-7", className)} {...props}>
      <span className="absolute left-2 flex size-icon items-center justify-center">
        <MenuPrimitive.ItemIndicator>
          <Check strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        </MenuPrimitive.ItemIndicator>
      </span>
      {children}
    </MenuPrimitive.CheckboxItem>
  );
});

export const DropdownMenuRadioItem = React.forwardRef<
  React.ElementRef<typeof MenuPrimitive.RadioItem>,
  React.ComponentPropsWithoutRef<typeof MenuPrimitive.RadioItem>
>(function DropdownMenuRadioItem({ className, children, ...props }, ref) {
  return (
    <MenuPrimitive.RadioItem ref={ref} className={cn(itemBase, "pl-7", className)} {...props}>
      <span className="absolute left-2 flex size-icon items-center justify-center">
        <MenuPrimitive.ItemIndicator>
          <Circle className="!size-2 fill-current" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        </MenuPrimitive.ItemIndicator>
      </span>
      {children}
    </MenuPrimitive.RadioItem>
  );
});

export const DropdownMenuLabel = React.forwardRef<
  React.ElementRef<typeof MenuPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof MenuPrimitive.Label>
>(function DropdownMenuLabel({ className, ...props }, ref) {
  return <MenuPrimitive.Label ref={ref} className={cn("px-2 py-1 text-caption font-medium text-muted-foreground", className)} {...props} />;
});

export const DropdownMenuSeparator = React.forwardRef<
  React.ElementRef<typeof MenuPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof MenuPrimitive.Separator>
>(function DropdownMenuSeparator({ className, ...props }, ref) {
  return <MenuPrimitive.Separator ref={ref} className={cn("-mx-1 my-1 h-px bg-border", className)} {...props} />;
});

export const DropdownMenuSubTrigger = React.forwardRef<
  React.ElementRef<typeof MenuPrimitive.SubTrigger>,
  React.ComponentPropsWithoutRef<typeof MenuPrimitive.SubTrigger>
>(function DropdownMenuSubTrigger({ className, children, ...props }, ref) {
  return (
    <MenuPrimitive.SubTrigger ref={ref} className={cn(itemBase, "data-[state=open]:bg-fill-active", className)} {...props}>
      {children}
      <ChevronRight className="ml-auto" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
    </MenuPrimitive.SubTrigger>
  );
});

export const DropdownMenuSubContent = React.forwardRef<
  React.ElementRef<typeof MenuPrimitive.SubContent>,
  React.ComponentPropsWithoutRef<typeof MenuPrimitive.SubContent>
>(function DropdownMenuSubContent({ className, ...props }, ref) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.SubContent ref={ref} className={cn(menuSurface, className)} {...props} />
    </MenuPrimitive.Portal>
  );
});
