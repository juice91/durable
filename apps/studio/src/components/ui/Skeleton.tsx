import React from "react";
import { cn } from "@/lib/cn";

/** A pulsing placeholder block. Decorative: the loading state is announced by its container. */
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div aria-hidden="true" data-skeleton="" className={cn("animate-pulse rounded-md bg-muted motion-reduce:animate-none", className)} {...props} />;
}
