"use client";

import React, { useEffect, useState } from "react";
import { listTools, type ToolDefinition } from "@/lib/tools";
import { McpToolPicker } from "../McpToolPicker";
import { stepForErrorMessage, stepStatus } from "../specSections";
import type { EditorStepDefinition, StepProps } from "./types";

/** Step 3: built-in tools and MCP tools. (Per-tool approval toggles: AC.13.) */
export function ToolsStep({ draft, onChange }: StepProps) {
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [toolsError, setToolsError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listTools()
      .then((items) => {
        if (!cancelled) setTools(items);
      })
      .catch((error: unknown) => {
        if (!cancelled) setToolsError(error instanceof Error ? error.message : "Failed to load tools");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const toggle = (id: string) => onChange({ tools: draft.tools.includes(id) ? draft.tools.filter((t) => t !== id) : [...draft.tools, id] });

  return (
    <div className="space-y-6">
      <fieldset className="min-w-0">
        <legend className="mb-2 text-label font-medium text-foreground">Built-in tools</legend>
        <div className="flex min-w-0 flex-wrap gap-2">
          {tools.map((tool) => (
            <label
              key={tool.id}
              className={`inline-flex min-w-0 max-w-full flex-wrap items-center gap-x-2 rounded-md border border-border bg-surface px-3 py-2 ${tool.allowed ? "cursor-pointer hover:bg-fill-hover" : "cursor-not-allowed opacity-60"}`}
            >
              <input
                type="checkbox"
                aria-label={`${tool.title} (${tool.id})`}
                className="size-4 accent-primary"
                checked={draft.tools.includes(tool.id)}
                disabled={!tool.allowed}
                onChange={() => toggle(tool.id)}
              />
              <span className="min-w-0 break-words text-label text-foreground">
                {tool.title} ({tool.id})
              </span>
              {!tool.allowed ? <span className="text-caption text-muted-foreground">Disabled by platform policy</span> : null}
            </label>
          ))}
        </div>
        {toolsError ? (
          <p role="alert" className="mt-2 text-caption text-destructive-text">
            {toolsError}
          </p>
        ) : null}
      </fieldset>

      <McpToolPicker value={draft.mcpTools} onChange={(mcpTools) => onChange({ mcpTools })} />
    </div>
  );
}

export const toolsStep: EditorStepDefinition = {
  id: "tools",
  label: "Tools",
  Component: ToolsStep,
  status: (spec) => stepStatus("tools", spec)!,
  ownsError: (message) => stepForErrorMessage(message) === "tools",
};
