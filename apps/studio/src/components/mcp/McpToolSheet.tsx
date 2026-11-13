"use client";

import React from "react";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import type { McpDiscoveredTool } from "@/lib/mcpServers";

interface McpToolSheetProps {
  tool: McpDiscoveredTool | null;
  serverName: string;
  onClose: () => void;
}

/**
 * A discovered tool, read-only: description and input schema. SL.13 replaces the body with the
 * Tools page's shared Overview.
 */
export function McpToolSheet({ tool, serverName, onClose }: McpToolSheetProps) {
  return (
    <Sheet open={tool !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      {tool ? (
        <SheetContent size="md" data-testid="mcp-tool-sheet">
          <SheetHeader title={<span className="font-mono">{tool.name}</span>} description={`Tool on ${serverName}`} />
          <SheetBody className="space-y-4">
            <section className="space-y-1">
              <h3 className="text-caption font-medium text-muted-foreground">Description</h3>
              <p className="text-body text-foreground">{tool.description || "No description."}</p>
            </section>
            <section className="space-y-1">
              <h3 className="text-caption font-medium text-muted-foreground">Input schema</h3>
              <pre data-testid="mcp-tool-schema" className="max-h-[60vh] overflow-auto rounded-md bg-muted p-3 font-mono text-caption text-foreground">{JSON.stringify(tool.input_schema, null, 2)}</pre>
            </section>
          </SheetBody>
        </SheetContent>
      ) : null}
    </Sheet>
  );
}
