"use client";

import React from "react";
import Link from "next/link";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { routes } from "@/lib/routes";
import type { McpServer } from "@/lib/mcpServers";
import { describeUsage, type McpAgentUsage, type McpUsageStatus } from "./mcpUsage";

interface McpDeleteConfirmProps {
  server: McpServer;
  usageStatus: McpUsageStatus;
  /** A fresh check is running: the list shown is the previous result, and Delete waits for the new one. */
  usageRefreshing?: boolean;
  /** Some latest versions could not be read: the list may be incomplete. */
  usagePartial?: boolean;
  usage: McpAgentUsage[];
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
  /** Where focus goes after a delete removed the opener (its row); defaults to the page's main. */
  fallbackFocus?: () => HTMLElement | null;
}

/** "Delete {name}?" with the agents whose draft or latest version uses the server's tools. */
export function McpDeleteConfirm({ server, usageStatus, usageRefreshing = false, usagePartial = false, usage, busy, error, onConfirm, onCancel, fallbackFocus }: McpDeleteConfirmProps) {
  let references: React.ReactNode;
  if (usageStatus === "loading") {
    references = <p>Checking which agents use it…</p>;
  } else if (usageStatus === "error") {
    references = <p>Could not check which agents use it. Agents that use its tools will fail to publish or run until they are changed.</p>;
  } else if (usage.length === 0) {
    references = <p>{usagePartial ? "No agent draft or readable published version uses its tools." : "No agent uses its tools."}</p>;
  } else {
    references = (
      <>
        <p>
          {usage.length === 1 ? "This agent uses" : `These ${usage.length} agents use`} its tools and will fail to publish or run until
          {usage.length === 1 ? " it is" : " they are"} changed:
        </p>
        <ul className="mt-2 space-y-1" data-testid="mcp-delete-reference-list">
          {usage.map((item) => (
            <li key={item.agentId} data-testid="mcp-delete-reference" className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              <Link href={routes.agentEditor(item.agentId)} className="font-medium text-foreground underline-offset-4 hover:underline">{item.agentName}</Link>
              <span className="text-caption">{describeUsage(item)}</span>
            </li>
          ))}
        </ul>
        {usagePartial ? <p className="mt-2" data-testid="mcp-delete-references-partial">Some published versions could not be read; other agents may also use it.</p> : null}
      </>
    );
  }
  return (
    <ConfirmDialog
      title={`Delete ${server.name}?`}
      description={
        <div className="space-y-2">
          <p>Its stored header secrets are deleted too.</p>
          <div data-testid="mcp-delete-references" data-status={usageStatus} data-refreshing={usageRefreshing || undefined} aria-busy={usageRefreshing || undefined}>
            {references}
            {usageRefreshing && usageStatus !== "loading" ? <p className="mt-2" data-testid="mcp-delete-references-rechecking">Re-checking…</p> : null}
          </div>
        </div>
      }
      confirmLabel="Delete server"
      destructive
      // Not until a fresh check has answered; on "error" it stays enabled, with the warning above.
      confirmDisabled={usageStatus === "loading" || usageRefreshing}
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      fallbackFocus={fallbackFocus}
      onCancel={onCancel}
    />
  );
}
