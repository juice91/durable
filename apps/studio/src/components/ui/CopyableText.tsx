"use client";

import React from "react";
import { cn } from "@/lib/cn";
import { CopyButton } from "./CopyButton";

export interface CopyableTextProps {
  children: React.ReactNode;
  /** The text to copy when it differs from what shows (raw JSON for a pretty-printed block). */
  copyText?: string;
  /** What is being copied, for the button's name ("Copy output"). */
  label?: string;
  className?: string;
  /** The copy button always visible (touch screens have no hover). */
  alwaysShow?: boolean;
  "data-testid"?: string;
}

/** Plain text of a React node tree, for when `copyText` is not given. */
function nodeText(node: React.ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (React.isValidElement(node)) return nodeText((node.props as { children?: React.ReactNode }).children);
  return "";
}

/**
 * A block of content that can be selected with the mouse and copied in one click: hovering or
 * focusing it shows a copy button at the top right.
 */
export function CopyableText({ children, copyText, label, className, alwaysShow = false, ...rest }: CopyableTextProps) {
  const read = () => copyText ?? nodeText(children);
  return (
    <div data-testid={rest["data-testid"]} data-copyable="" className={cn("group/copy relative min-w-0 select-text", className)}>
      {children}
      <span
        className={cn(
          "absolute right-1 top-1 transition-opacity",
          alwaysShow ? "opacity-100" : "opacity-0 focus-within:opacity-100 group-hover/copy:opacity-100 group-focus-within/copy:opacity-100",
        )}
      >
        <CopyButton text={read} label={label} className="bg-surface/80 backdrop-blur-sm" />
      </span>
    </div>
  );
}
