import React from "react";
import { cn } from "@/lib/cn";

/** A keyboard key or chord as shown in tooltips, menus and the shortcut help ("⌘K", "G A"). */
export function Kbd({ className, ...props }: React.HTMLAttributes<HTMLElement>) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center gap-0.5 rounded-sm border border-border bg-muted px-1 font-mono text-badge font-medium text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}
