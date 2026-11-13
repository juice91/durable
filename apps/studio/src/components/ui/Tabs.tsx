"use client";

import React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import { cn } from "@/lib/cn";
import { focusRing } from "./Button";

export const Tabs = TabsPrimitive.Root;

const listClass = "flex min-w-0 items-center gap-1 overflow-x-auto border-b border-border";
const tabClass = cn(
  "relative inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-t-sm px-2 text-label font-medium text-muted-foreground transition-colors motion-reduce:transition-none hover:text-foreground",
  "after:absolute after:inset-x-0 after:-bottom-px after:h-0.5 after:bg-transparent [&_svg]:size-icon",
  focusRing,
);
const activeTabClass = "text-foreground after:bg-foreground";

export const TabsList = React.forwardRef<React.ElementRef<typeof TabsPrimitive.List>, React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>>(
  function TabsList({ className, ...props }, ref) {
    return <TabsPrimitive.List ref={ref} className={cn(listClass, className)} {...props} />;
  },
);

export const TabsTrigger = React.forwardRef<React.ElementRef<typeof TabsPrimitive.Trigger>, React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>>(
  function TabsTrigger({ className, ...props }, ref) {
    return (
      <TabsPrimitive.Trigger
        ref={ref}
        className={cn(tabClass, "data-[state=active]:text-foreground data-[state=active]:after:bg-foreground disabled:pointer-events-none disabled:opacity-50", className)}
        {...props}
      />
    );
  },
);

export const TabsContent = React.forwardRef<React.ElementRef<typeof TabsPrimitive.Content>, React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>>(
  function TabsContent({ className, ...props }, ref) {
    return <TabsPrimitive.Content ref={ref} className={cn("min-w-0 outline-none focus-visible:ring-2 focus-visible:ring-ring", className)} {...props} />;
  },
);

export interface LinkTab {
  href: string;
  label: React.ReactNode;
  /** Overrides the pathname match. */
  active?: boolean;
  /** Small count after the label. */
  count?: number;
  icon?: React.ReactNode;
  testId?: string;
}

export interface LinkTabsProps {
  items: LinkTab[];
  /** Accessible name of the tab navigation ("Agent sections"). */
  label: string;
  /** The current path; defaults to `usePathname()`. */
  pathname?: string | null;
  className?: string;
}

/** True when `pathname` is `href`'s path or below it (query strings are ignored). The root `/` matches only itself. */
export function isLinkTabActive(href: string, pathname: string | null | undefined): boolean {
  if (!pathname) return false;
  const path = href.split(/[?#]/)[0] || "/";
  if (path === "/") return pathname === "/";
  return pathname === path || pathname.startsWith(path.endsWith("/") ? path : `${path}/`);
}

/**
 * Tabs that are links (route tabs: Chat · Traces · Editor). A `nav` landmark with `aria-current="page"`
 * on the active tab: navigation semantics, not an ARIA tablist, because each tab is its own URL.
 */
export function LinkTabs({ items, label, pathname, className }: LinkTabsProps) {
  const currentPath = usePathname();
  const path = pathname ?? currentPath;
  return (
    <nav aria-label={label} className={cn(listClass, "border-b-0", className)}>
      {items.map((item) => {
        const active = item.active ?? isLinkTabActive(item.href, path);
        return (
          <Link key={item.href} href={item.href} aria-current={active ? "page" : undefined} data-testid={item.testId} className={cn(tabClass, active && activeTabClass)}>
            {item.icon}
            {item.label}
            {item.count !== undefined ? <span className="text-caption tabular-nums text-muted-foreground">{item.count}</span> : null}
          </Link>
        );
      })}
    </nav>
  );
}
