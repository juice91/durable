import React from "react";
import Link from "next/link";
import { routes } from "@/lib/routes";

/** Servers · Browse, beside each other on both pages. Plain links (no router hook): the page says which is current. */
export function McpSectionTabs({ current }: { current: "servers" | "browse" }) {
  const base = "inline-flex items-center rounded-md px-3 py-1.5 text-body";
  const active = "bg-accent font-medium text-foreground";
  const idle = "text-muted-foreground hover:text-foreground";
  return (
    <nav aria-label="MCP sections" className="flex items-center gap-1">
      <Link href={routes.mcp()} aria-current={current === "servers" ? "page" : undefined} className={`${base} ${current === "servers" ? active : idle}`}>Servers</Link>
      <Link href={routes.mcpBrowse()} aria-current={current === "browse" ? "page" : undefined} className={`${base} ${current === "browse" ? active : idle}`}>Browse</Link>
    </nav>
  );
}
