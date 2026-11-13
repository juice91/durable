"use client";

import React from "react";
import Link from "next/link";
import { EmptyState } from "@/components/shared/EmptyState";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/Tabs";
import type { AgentSpecsStatus, AgentUsage } from "@/lib/agentUsage";
import { routes } from "@/lib/routes";
import type { ToolTab } from "@/lib/routes/tools";
import { RunsStartedElsewhere } from "./RunsStartedElsewhere";
import { ToolOverview } from "./ToolOverview";
import { ToolRunner } from "./ToolRunner";
import { sourceLabel, type ToolRow } from "./toolCatalog";

export interface ToolSheetProps {
  /** `?tool=`: null when the Sheet is closed. */
  name: string | null;
  /** `?server=` (MCP tools). */
  server: string | null;
  /** The row `name` points at; null while loading or when it does not exist. */
  row: ToolRow | null;
  /** Still loading what `name` could point at. */
  loading: boolean;
  tab: ToolTab;
  onTabChange: (tab: ToolTab) => void;
  onOpenChange: (open: boolean) => void;
  usedBy: AgentUsage[];
  usageStatus: AgentSpecsStatus;
  usagePartial: boolean;
  onRetryUsage: () => void;
  /** `GET /tools` failed: a built-in tool cannot be looked up. */
  loadError: string | null;
  onRetryLoad: () => void;
}

/**
 * One tool in a right-hand Sheet (full width below 768 px), addressed by `?tool=`: tabs Overview
 * (read-only) and Run (built-in tools only). The Run tab stays mounted while Overview is shown, so
 * switching tabs keeps the input and never orphans a request in flight.
 */
export function ToolSheet({ name, server, row, loading, tab, onTabChange, onOpenChange, usedBy, usageStatus, usagePartial, onRetryUsage, loadError, onRetryLoad }: ToolSheetProps) {
  const open = name !== null;
  const runnable = row?.definition ?? null;
  const activeTab: ToolTab = runnable ? tab : "overview";
  const title = row?.title ?? name ?? "";
  const description = row
    ? row.source.kind === "builtin"
      ? <span className="font-mono">{row.name}</span>
      : <>MCP tool on <span className="font-mono">{row.source.server}</span></>
    : loading ? "Loading…" : null;
  // Built-in tools come from the catalog, MCP tools from the agents scan: when the one that would
  // hold this tool failed to load, say so (with Retry) instead of claiming it does not exist.
  const failure = row || loading
    ? null
    : server
      ? usageStatus === "error" ? { title: "Couldn't load agents", description: "MCP tools are listed from agent specs, and the agents could not be loaded.", retry: onRetryUsage } : null
      : loadError !== null ? { title: "Couldn't load tools", description: loadError, retry: onRetryLoad } : null;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      {open ? (
        <SheetContent size="md" data-testid="tool-sheet" data-tool={name ?? undefined}>
          <SheetHeader
            title={title}
            description={description ?? (failure ? "Couldn't load" : "Not found")}
            actions={row ? <Badge variant={row.source.kind === "builtin" ? "neutral" : "info"} className="hidden sm:inline-flex">{sourceLabel(row.source)}</Badge> : null}
          />
          <SheetBody className="min-w-0 space-y-4">
            <RunsStartedElsewhere />
            {row ? (
              <Tabs value={activeTab} onValueChange={(value) => onTabChange(value === "run" ? "run" : "overview")}>
                <TabsList aria-label="Tool sections">
                  <TabsTrigger value="overview" data-testid="tool-tab-overview">Overview</TabsTrigger>
                  {runnable ? <TabsTrigger value="run" data-testid="tool-tab-run">Run</TabsTrigger> : null}
                </TabsList>
                <TabsContent value="overview" className="pt-4">
                  <ToolOverview
                    description={row.description}
                    inputSchema={runnable ? (runnable.input_schema as Record<string, unknown>) : null}
                    inputSchemaNote={
                      row.source.kind === "mcp" ? (
                        <>
                          The server describes its tools when a connection test runs. Test{" "}
                          <span className="font-mono">{row.source.server}</span> on the{" "}
                          <Link href={routes.mcp()} className="text-foreground underline underline-offset-4 hover:no-underline">MCP Servers</Link> page to see the schema.
                        </>
                      ) : undefined
                    }
                    policy={runnable ? { sideEffect: runnable.side_effect, allowed: runnable.allowed } : null}
                    usedBy={usedBy}
                    usageStatus={usageStatus}
                    usagePartial={usagePartial}
                    onRetryUsage={onRetryUsage}
                  />
                </TabsContent>
                {runnable ? (
                  // `hidden` (not only a class): forceMount drops Radix's own, and the form must leave the tab order.
                  <TabsContent value="run" forceMount hidden={activeTab !== "run"} className="pt-4 data-[state=inactive]:hidden">
                    <ToolRunner key={runnable.id} tool={runnable} />
                  </TabsContent>
                ) : null}
              </Tabs>
            ) : loading ? (
              <p className="text-body text-muted-foreground" aria-live="polite">Loading tool…</p>
            ) : failure ? (
              <EmptyState
                compact
                testId="tool-load-error"
                title={failure.title}
                description={failure.description}
                action={<Button size="sm" onClick={failure.retry}>Retry</Button>}
              />
            ) : (
              <EmptyState
                compact
                testId="tool-not-found"
                title="Tool not found"
                description={
                  server
                    ? <>No agent uses a tool named <span className="font-mono">{name}</span> on <span className="font-mono">{server}</span>.</>
                    : <>The catalog has no tool named <span className="font-mono">{name}</span>.</>
                }
              />
            )}
          </SheetBody>
        </SheetContent>
      ) : null}
    </Sheet>
  );
}
