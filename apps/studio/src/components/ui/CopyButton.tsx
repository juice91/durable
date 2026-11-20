"use client";

import React, { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { copyText } from "@/lib/clipboard";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { cn } from "@/lib/cn";
import { IconButton } from "./IconButton";
import { toast } from "./Toaster";

/** How long the "Copied" state shows. */
export const COPIED_MS = 1500;

export interface CopyButtonProps {
  /** What to copy. A function is read at click time (pretty JSON need not be built on every render). */
  text: string | (() => string);
  /** What is being copied ("input"): the name becomes "Copy input". Omit for a bare "Copy". */
  label?: string;
  size?: "sm" | "md";
  className?: string;
  "data-testid"?: string;
}

/**
 * An icon button that copies `text`. It writes with the async clipboard API and falls back to a
 * hidden textarea and `execCommand("copy")`; the icon becomes a check for 1.5 s ("Copied", also
 * announced politely). A toast shows only when copying failed, so a success never adds noise.
 */
export function CopyButton({ text, label, size = "sm", className, ...rest }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const onClick = async (event: React.MouseEvent) => {
    event.stopPropagation();
    const ok = await copyText(typeof text === "function" ? text() : text);
    if (!ok) {
      toast("Could not copy; select the text and copy it.");
      return;
    }
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), COPIED_MS);
  };
  const Icon = copied ? Check : Copy;
  return (
    <>
      <IconButton
        label={label ? `Copy ${label}` : "Copy"}
        size={size}
        tooltip={false}
        data-copy-button=""
        data-copied={copied ? "true" : undefined}
        data-testid={rest["data-testid"]}
        className={cn("text-muted-foreground hover:text-foreground", className)}
        icon={<Icon strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className={copied ? "text-success" : undefined} />}
        onClick={(e) => void onClick(e)}
        onMouseDown={(e) => e.stopPropagation()}
      />
      <span role="status" className="sr-only">{copied ? "Copied" : ""}</span>
    </>
  );
}
