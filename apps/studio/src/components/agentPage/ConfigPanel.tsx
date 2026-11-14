"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import type { Agent, AgentVersion } from "@/lib/api";
import { routes } from "@/lib/routes";
import { cn } from "@/lib/cn";
import { Badge } from "@/components/ui/Badge";
import { Button, focusRing } from "@/components/ui/Button";
import { sectionsOf, type CoreStepId } from "@/components/agents/specSections";
import type { VersionRef } from "@/components/chat/runOptions";
import { configCounts, effectiveConfig, instructionSegments } from "./agentPageModel";
import { copyText } from "@/lib/clipboard";
import { listKnowledgeBases } from "@/lib/knowledge";
import { knowledgeBasesOf, knowledgeRefSummary, memorySummaryLines } from "@/components/agents/knowledgeMemory";

/** Editor steps the "Edit" links open (the stepper's `?step=` ids, `stepper/registry.ts`). */
export type ConfigStep = CoreStepId | (string & {});

export interface ConfigPanelProps {
  agent: Agent;
  versions: AgentVersion[] | null;
  /** The version the chat targets; null: the latest published (the draft when none is). */
  versionRef?: VersionRef;
}

function Section({
  id,
  title,
  count,
  editHref,
  children,
}: {
  id: string;
  title: string;
  count?: number;
  editHref?: string;
  children: React.ReactNode;
}) {
  const headingId = `config-${id}-title`;
  return (
    <section aria-labelledby={headingId} data-testid={`config-section-${id}`} className="border-b border-border px-3 py-3 last:border-b-0">
      <div className="mb-1.5 flex items-center gap-2">
        <h3 id={headingId} className="min-w-0 flex-1 truncate text-label font-semibold text-foreground">
          {title}
          {/* A real space keeps the count apart in the accessible name ("Tools 1", not "Tools1"). */}
          {count !== undefined ? (
            <>
              {" "}
              <span className="ml-1 text-caption font-normal tabular-nums text-muted-foreground">{count}</span>
            </>
          ) : null}
        </h3>
        {editHref ? (
          <Link href={editHref} aria-label={`Edit ${title.toLowerCase()}`} className={cn("rounded-sm text-caption text-muted-foreground hover:text-foreground hover:underline", focusRing)}>
            Edit
          </Link>
        ) : null}
      </div>
      <div className="min-w-0 text-label text-foreground">{children}</div>
    </section>
  );
}

const muted = "text-muted-foreground";

/**
 * The read-only Config panel (decision "Agent chat panes", right pane): what the agent the chat runs
 * is made of, from the effective spec (latest published, or the draft with `?version=draft`). Each
 * section links to its editor step.
 */
export function ConfigPanel({ agent, versions, versionRef = null }: ConfigPanelProps) {
  const config = effectiveConfig(agent, versions, versionRef);
  const spec = config.spec;
  const counts = configCounts(spec);
  const sections = sectionsOf(spec ?? undefined);
  const [wrap, setWrap] = useState(true);
  const [copied, setCopied] = useState<string | null>(null);
  const edit = (step: ConfigStep) => routes.agentEditor(agent.id, { step });
  const knowledgeRefs = knowledgeBasesOf(spec);
  const memoryLines = memorySummaryLines(spec);
  const kbNames = useKnowledgeBaseNames(knowledgeRefs.length > 0);

  const copyInstructions = async () => {
    setCopied((await copyText(spec?.instructions ?? "")) ? "Instructions copied" : "Copying is not available here");
  };

  return (
    <div data-testid="config-panel" className="min-w-0">
      <Section id="version" title="Version" editHref={routes.agentEditor(agent.id)}>
        <p className="flex flex-wrap items-center gap-1.5">
          <span data-testid="config-version">{config.versionLabel}</span>
          {config.isDraft ? <Badge variant="warning">Unpublished</Badge> : null}
          {config.restoredFrom ? <span className={muted}>restored from v{config.restoredFrom}</span> : null}
        </p>
      </Section>

      {spec === null ? (
        <p className={cn("px-3 py-3 text-label", muted)} role="status">
          Loading configuration…
        </p>
      ) : (
        <>
          <Section id="model" title="Model" editHref={edit("identity")}>
            <p className="break-all font-mono text-caption" data-testid="config-model">
              {spec.model.provider}:{spec.model.name}
            </p>
            {spec.model_settings?.temperature !== undefined && spec.model_settings?.temperature !== null ? (
              <p className={cn("text-caption", muted)}>Temperature {spec.model_settings.temperature}</p>
            ) : null}
            {spec.model_settings?.max_tokens ? <p className={cn("text-caption", muted)}>Max tokens {spec.model_settings.max_tokens}</p> : null}
          </Section>

          <Section id="instructions" title="Instructions" editHref={edit("instructions")}>
            <div className="mb-1.5 flex items-center gap-1">
              <Button size="sm" variant="ghost" aria-pressed={wrap} onClick={() => setWrap((w) => !w)}>
                Wrap
              </Button>
              <Button size="sm" variant="ghost" onClick={copyInstructions} disabled={!spec.instructions}>
                Copy
              </Button>
              <span role="status" className={cn("text-caption", muted)}>
                {copied}
              </span>
            </div>
            {spec.instructions ? (
              <pre
                data-testid="config-instructions"
                className={cn(
                  "max-h-64 overflow-auto rounded-md border border-border bg-muted p-2 font-mono text-caption",
                  wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre",
                )}
              >
                {instructionSegments(spec.instructions).map((segment, index) =>
                  segment.placeholder ? (
                    <mark key={index} data-placeholder="" className="rounded-sm bg-info/15 px-0.5 text-info-text">
                      {segment.text}
                    </mark>
                  ) : (
                    <React.Fragment key={index}>{segment.text}</React.Fragment>
                  ),
                )}
              </pre>
            ) : (
              <p className={muted}>No instructions</p>
            )}
          </Section>

          <Section id="tools" title="Tools" count={counts.tools} editHref={edit("tools")}>
            {counts.tools === 0 ? (
              <p className={muted}>None</p>
            ) : (
              <ul className="space-y-0.5 font-mono text-caption">
                {spec.tools.map((tool) => (
                  <li key={`builtin:${tool}`} className="break-all">
                    {tool}
                  </li>
                ))}
                {(spec.mcp_tools ?? []).map((ref) => (
                  <li key={`mcp:${ref.server}:${ref.tool}`} className="break-all">
                    {ref.tool} <span className={muted}>· MCP {ref.server}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section id="context-fields" title="Request context fields" count={counts.contextFields} editHref={edit("request-context")}>
            {sections.context_fields.length === 0 ? (
              <p className={muted}>None</p>
            ) : (
              <ul className="space-y-0.5 text-caption">
                {sections.context_fields.map((field) => (
                  <li key={field.name} className="break-all">
                    <span className="font-mono">{field.name}</span> <span className={muted}>{field.type}{field.required ? ", required" : ""}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section id="processors" title="Processors" count={counts.processors} editHref={edit("processors")}>
            {counts.processors === 0 ? (
              <p className={muted}>None</p>
            ) : (
              <dl className="space-y-1 text-caption">
                {(["input", "output"] as const).map((phase) => {
                  const list = phase === "input" ? sections.input_processors : sections.output_processors;
                  return (
                    <div key={phase}>
                      <dt className={muted}>{phase === "input" ? "Input" : "Output"}</dt>
                      <dd className="font-mono">{list.length === 0 ? <span className={muted}>None</span> : list.map((p) => p.type).join(", ")}</dd>
                    </div>
                  );
                })}
              </dl>
            )}
          </Section>

          <Section id="knowledge" title="Knowledge" count={knowledgeRefs.length} editHref={edit("knowledge")}>
            {knowledgeRefs.length === 0 ? (
              <p className={muted}>None</p>
            ) : (
              <ul className="space-y-1 text-caption" data-testid="config-knowledge">
                {knowledgeRefs.map((ref, index) => (
                  <li key={`${ref.id}-${index}`} className="min-w-0">
                    <span className="block break-words">{kbNames?.get(ref.id) ?? <span className="break-all font-mono">{ref.id}</span>}</span>
                    <span className={cn("block break-words", muted)}>{knowledgeRefSummary(ref)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section id="memory" title="Memory" editHref={edit("memory")}>
            {memoryLines.length === 0 ? (
              <p className={muted}>Off</p>
            ) : (
              <dl className="space-y-1 text-caption" data-testid="config-memory">
                {memoryLines.map(([label, detail]) => (
                  <div key={label} className="min-w-0">
                    <dt className={muted}>{label}</dt>
                    <dd className="break-words">{detail}</dd>
                  </div>
                ))}
              </dl>
            )}
          </Section>
        </>
      )}
    </div>
  );
}

/** Knowledge base names by id, loaded only when the spec attaches any (null until loaded or on failure: ids show). */
function useKnowledgeBaseNames(needed: boolean): Map<string, string> | null {
  const [names, setNames] = useState<Map<string, string> | null>(null);
  useEffect(() => {
    if (!needed || names) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => listKnowledgeBases())
      .then((items) => {
        if (!cancelled) setNames(new Map((items ?? []).map((kb) => [kb.id, kb.name])));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [needed, names]);
  return names;
}
