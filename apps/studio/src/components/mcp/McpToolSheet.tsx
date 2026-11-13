"use client";

import React from "react";
import { ToolOverview } from "@/components/tools/ToolOverview";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import type { AgentSpecsStatus, AgentUsage } from "@/lib/agentUsage";
import type { McpDiscoveredTool } from "@/lib/mcpServers";

interface McpToolSheetProps {
  tool: McpDiscoveredTool | null;
  serverName: string;
  onClose: () => void;
  /** The agents whose draft or latest version uses this tool of this server. */
  usedBy?: AgentUsage[];
  usageStatus?: AgentSpecsStatus;
  usagePartial?: boolean;
  onRetryUsage?: () => void;
}

/** A discovered tool, read-only: the Tools page's Overview (description, schemas, used by). */
export function McpToolSheet({ tool, serverName, onClose, usedBy = [], usageStatus = "ready", usagePartial = false, onRetryUsage }: McpToolSheetProps) {
  return (
    <Sheet open={tool !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      {tool ? (
        <SheetContent size="md" data-testid="mcp-tool-sheet">
          <SheetHeader title={<span className="font-mono">{tool.name}</span>} description={`Tool on ${serverName}`} />
          <SheetBody>
            <ToolOverview
              description={tool.description}
              inputSchema={tool.input_schema}
              usedBy={usedBy}
              usageStatus={usageStatus}
              usagePartial={usagePartial}
              onRetryUsage={onRetryUsage}
              context="mcp"
            />
          </SheetBody>
        </SheetContent>
      ) : null}
    </Sheet>
  );
}
