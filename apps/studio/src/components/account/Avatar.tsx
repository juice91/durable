"use client";

import React, { useEffect, useState } from "react";
import { cn } from "@/lib/cn";

const SIZES = { sm: "size-6", lg: "size-10" } as const;

/**
 * A round, fixed-size avatar: the picture when there is one and it loads, otherwise the first letter of the name.
 * The box is the same size either way, so a late or failed picture moves nothing.
 */
export function Avatar({ name, src, size = "sm", className }: { name: string; src?: string | null; size?: keyof typeof SIZES; className?: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  const initial = (name.trim().charAt(0) || "?").toUpperCase();
  const box = cn("relative flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-fill-hover font-medium text-foreground", SIZES[size], size === "lg" ? "text-body" : "text-caption", className);
  if (src && !failed) {
    return (
      <span className={box} data-testid="avatar-picture">
        {/* eslint-disable-next-line @next/next/no-img-element -- a blob or a provider URL: next/image cannot optimise it */}
        <img src={src} alt={`${name}'s picture`} referrerPolicy="no-referrer" width={size === "lg" ? 40 : 24} height={size === "lg" ? 40 : 24} onError={() => setFailed(true)} className="size-full object-cover" />
      </span>
    );
  }
  return (
    <span aria-hidden="true" className={box} data-testid="avatar-initial">
      {initial}
    </span>
  );
}
