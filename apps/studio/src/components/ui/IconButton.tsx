"use client";

import React from "react";
import { cn } from "@/lib/cn";
import { Button, type ButtonProps } from "./Button";
import { Tooltip } from "./Tooltip";

export interface IconButtonProps extends Omit<ButtonProps, "children" | "aria-label"> {
  /** The accessible name, also shown as the tooltip. Required: an icon alone has no name. */
  label: string;
  /** The icon element (decorative). */
  icon: React.ReactNode;
  /** A shortcut shown in the tooltip. */
  shortcut?: string;
  /** Set false to skip the tooltip (the label stays as `aria-label`). */
  tooltip?: boolean;
  tooltipSide?: "top" | "right" | "bottom" | "left";
}

/** A square icon-only button with an `aria-label` and a tooltip. 28 px (sm) or 32 px (md). */
export const IconButton = React.forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, shortcut, tooltip = true, tooltipSide, variant = "ghost", size = "md", className, ...props },
  ref,
) {
  const button = (
    <Button ref={ref} aria-label={label} variant={variant} size={size} className={cn("px-0", size === "sm" ? "size-7" : size === "lg" ? "size-9" : "size-8", className)} {...props}>
      {icon}
    </Button>
  );
  return tooltip ? <Tooltip content={label} shortcut={shortcut} side={tooltipSide}>{button}</Tooltip> : button;
});
