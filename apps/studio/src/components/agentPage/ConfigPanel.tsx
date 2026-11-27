"use client";

import { EXTRA_SETTING_KEYS, MODEL_SETTING_FIELDS } from "@/lib/modelSettings";
import React, { useEffect, useState } from "react";
import Link from "next/link";
import type { Agent, AgentVersion } from "@/lib/api";
import { routes } from "@/lib/routes";
import { PROCESSOR_PHASES, PROCESSOR_PHASE_LABELS } from "@/lib/processors";
import { cn } from "@/lib/cn";
import { Badge } from "@/components/ui/Badge";
import { AccessSection } from "@/components/access/AccessSection";
import { DraftDirtyBadge } from "@/components/agents/DraftDirtyBadge";
import { Button, focusRing } from "@/components/ui/Button";
import { sectionsOf, type CoreStepId } from "@/components/agents/specSections";
import type { VersionRef } from "@/components/chat/runOptions";
import type { RunStatus } from "@/lib/runs";
import { configCounts, effectiveConfig, instructionSegments } from "./agentPageModel";
import { copyText } from "@/lib/clipboard";
import { listKnowledgeBases } from "@/lib/knowledge";
import { listAgentScorers, type AttachedScorer } from "@/lib/scorers";
import { knowledgeBasesOf, knowledgeRefSummary, memorySummaryLines } from "@/components/agents/knowledgeMemory";
import {
  approvalsOf,
  DEFAULT_APPROVAL_TIMEOUT_HOURS,
  effectiveOutputMode,
  fallbackModelsOf,
  LIMIT_FIELDS,
  limitsOf,
  mcpApprovalRef,
  outputModeLabel,
  outputOf,
  schemaTypeLabel,
} from "@/components/agents/agentCapabilities";

/** Editor steps the "Edit" links open (the stepper's `?step=` ids, `stepper/registry.ts`). */
export type ConfigStep = CoreStepId | (string & {});

export interface ConfigPanelProps {
  agent: Agent;
  versions: AgentVersion[] | null;
  /** The version the chat targets; null: the latest published (the draft when none is). */
  versionRef?: VersionRef;
  /** AC.14: the chat's latest turn status; "waiting_approval" shows a pill in the header. */
  turnStatus?: RunStatus | null;
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
export function ConfigPanel({ agent, versions, versionRef = null, turnStatus = null }: ConfigPanelProps) {
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
  const scorers = useAgentScorers(agent.id);
  // AC.13: fallback models and limits under Model, approval marks on tools, and the Output section.
  const fallbacks = fallbackModelsOf(spec);
  const limits = limitsOf(spec);
  const limitLines = limits
    ? [
        ...LIMIT_FIELDS.filter(({ key }) => limits[key] != null).map(({ key, label }) => `${label} ≤ ${Number(limits[key]).toLocaleString("en-US")}`),
        ...(limits.max_cost_usd != null ? [`Cost ≤ $${String(limits.max_cost_usd)}`] : []),
      ]
    : [];
  const approvals = approvalsOf(spec);
  const gated = new Set(approvals?.tools ?? []);
  const output = outputOf(spec);
  const outputSchema = output?.json_schema as { properties?: Record<string, unknown>; required?: string[] } | undefined;
  const outputFields = outputSchema && typeof outputSchema === "object" && outputSchema.properties && typeof outputSchema.properties === "object" ? Object.entries(outputSchema.properties) : [];

  const copyInstructions = async () => {
    setCopied((await copyText(spec?.instructions ?? "")) ? "Instructions copied" : "Copying is not available here");
  };

  return (
    <div data-testid="config-panel" className="min-w-0">
      {turnStatus === "waiting_approval" ? (
        <p role="status" data-testid="config-waiting-approval" className="flex items-center gap-2 border-b border-border px-3 py-2 text-caption">
          <Badge variant="warning">Waiting for approval</Badge>
          <span className={muted}>A tool call waits for a decision in the chat.</span>
        </p>
      ) : null}
      <Section id="version" title="Version" editHref={routes.agentEditor(agent.id)}>
        <p className="flex flex-wrap items-center gap-1.5">
          <span data-testid="config-version">{config.versionLabel}</span>
          {config.isDraft ? <Badge variant="warning">Unpublished</Badge> : null}
          <DraftDirtyBadge dirty={agent.draft_dirty} version={agent.latest_version} />
          {config.restoredFrom ? <span className={muted}>restored from v{config.restoredFrom}</span> : null}
        </p>
      </Section>

      <AccessSection kind="agent" id={agent.id} heading={false} wrapper={(content) => <Section id="access" title="Access">{content}</Section>} />

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
            {EXTRA_SETTING_KEYS.map((key) => {
              const value = spec.model_settings?.[key];
              return typeof value === "number" ? (
                <p key={key} className={cn("text-caption", muted)}>
                  {MODEL_SETTING_FIELDS.find((field) => field.key === key)?.label} {value}
                </p>
              ) : null;
            })}
            {fallbacks.length > 0 ? (
              <div className="mt-1.5 text-caption" data-testid="config-fallback-models">
                <p className={muted}>Fallback models</p>
                <ol className="list-inside list-decimal">
                  {fallbacks.map((ref, index) => (
                    <li key={`${ref.provider}:${ref.name}:${index}`} className="break-all font-mono">
                      {ref.provider}:{ref.name}
                    </li>
                  ))}
                </ol>
              </div>
            ) : null}
            {limitLines.length > 0 ? (
              <div className="mt-1.5 text-caption" data-testid="config-limits">
                <p className={muted}>Limits per run</p>
                <ul>
                  {limitLines.map((line) => (
                    <li key={line} className="break-words">
                      {line}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
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
                    {gated.has(tool) ? <ApprovalMark /> : null}
                  </li>
                ))}
                {(spec.mcp_tools ?? []).map((ref) => (
                  <li key={`mcp:${ref.server}:${ref.tool}`} className="break-all">
                    {ref.tool} <span className={muted}>· MCP {ref.server}</span>
                    {gated.has(mcpApprovalRef(ref)) ? <ApprovalMark /> : null}
                  </li>
                ))}
              </ul>
            )}
            {gated.size > 0 ? (
              <p className={cn("mt-1 text-caption", muted)} data-testid="config-approval-timeout">
                Approval timeout {String(approvals?.timeout_hours ?? DEFAULT_APPROVAL_TIMEOUT_HOURS)} h
              </p>
            ) : null}
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
                {PROCESSOR_PHASES.map((phase) => {
                  const list = sections[`${phase}_processors`];
                  return (
                    <div key={phase}>
                      <dt className={muted}>{PROCESSOR_PHASE_LABELS[phase]}</dt>
                      <dd className="font-mono">{list.length === 0 ? <span className={muted}>None</span> : list.map((p) => p.type).join(", ")}</dd>
                    </div>
                  );
                })}
              </dl>
            )}
          </Section>

          <Section id="scorers" title="Scorers" count={scorers.items?.length} editHref={routes.agentEditor(agent.id)}>
            {scorers.error ? (
              <p className={muted}>Could not load the scorers</p>
            ) : scorers.items === null ? (
              <p className={muted}>Loading…</p>
            ) : scorers.items.length === 0 ? (
              <p className={muted}>None</p>
            ) : (
              <ul className="space-y-0.5 text-caption" data-testid="config-scorers">
                {scorers.items.map((scorer) => (
                  <li key={scorer.id} className="break-words">
                    {scorer.name} · {Math.round(scorer.sampling_rate * 100)}%
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section id="knowledge" title="Knowledge" count={knowledgeRefs.length} editHref={edit("knowledge")}>
            {knowledgeRefs.length === 0 ? (
              <p className={muted}>None</p>
            ) : (
              <ul className="space-y-1 text-caption" data-testid="config-knowledge">
                {knowledgeRefs.map((ref, index) => (
                  <li key={`${ref.id}-${index}`} className="min-w-0">
                    <span className="block break-words">
                      {kbNames?.get(ref.id) ??
                        (kbNames ? (
                          // The list is the tenant's every knowledge base (not paged): absent is deleted.
                          <span data-testid="config-knowledge-deleted" title={ref.id}>
                            Deleted knowledge base <span className={cn("font-mono", muted)}>{shortId(ref.id)}</span>
                          </span>
                        ) : (
                          <span className="break-all font-mono">{ref.id}</span>
                        ))}
                    </span>
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
            {memoryLines.length > 0 ? (
              // RM.18: each user's memory lives on the Memory tab.
              <Link href={routes.agentMemory(agent.id)} data-testid="config-memory-open" className={cn("mt-2 inline-block rounded-sm text-caption text-info-text underline-offset-4 hover:underline", focusRing)}>
                Memory per user
              </Link>
            ) : null}
          </Section>

          <Section id="output" title="Output" count={output ? outputFields.length : undefined} editHref={edit("output")}>
            {output ? (
              <div className="text-caption" data-testid="config-output">
                <p>
                  Structured · {outputModeLabel(effectiveOutputMode(output))}
                </p>
                {outputFields.length === 0 ? (
                  <p className={muted}>No fields</p>
                ) : (
                  <ul className="mt-0.5 space-y-0.5">
                    {outputFields.map(([name, node]) => (
                      <li key={name} className="break-all">
                        <span className="font-mono">{name}</span>{" "}
                        <span className={muted}>
                          {schemaTypeLabel(node)}
                          {outputSchema?.required?.includes(name) ? ", required" : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ) : (
              <p className={muted} data-testid="config-output-text">
                Text
              </p>
            )}
          </Section>
        </>
      )}
    </div>
  );
}

/** The mark on a tool that requires approval. */
function ApprovalMark() {
  return (
    <>
      {" "}
      <Badge variant="warning" data-testid="config-approval-mark">
        Approval
      </Badge>
    </>
  );
}

/** The first 8 characters of an id ("1a2b3c4d…"), for a row that has no name to show. */
function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

/** The agent's attached scorers (a failed load is shown as such, never thrown). */
function useAgentScorers(agentId: string): { items: AttachedScorer[] | null; error: boolean } {
  const [state, setState] = useState<{ items: AttachedScorer[] | null; error: boolean }>({ items: null, error: false });
  useEffect(() => {
    const controller = new AbortController();
    setState({ items: null, error: false });
    Promise.resolve()
      .then(() => listAgentScorers(agentId, controller.signal))
      .then((items) => {
        if (!controller.signal.aborted) setState({ items: Array.isArray(items) ? items : [], error: false });
      })
      .catch(() => {
        if (!controller.signal.aborted) setState({ items: null, error: true });
      });
    return () => controller.abort();
  }, [agentId]);
  return state;
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
