"use client";

import React, { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, type Agent } from "@/lib/api";
import { routes } from "@/lib/routes";
import { Button } from "@/components/ui/Button";
import { Combobox } from "@/components/ui/Combobox";
import { Select } from "@/components/ui/Select";
import {
  agentToolProblems,
  agentToolsOf,
  agentToolsValue,
  DEFAULT_MAX_CALLS_PER_RUN,
  DEFAULT_TIMEOUT_SECONDS,
  defaultToolName,
  MAX_AGENT_TOOLS_PER_AGENT,
  MAX_CALLS_PER_RUN_LIMIT,
  MAX_DESCRIPTION_CHARS,
  MAX_TIMEOUT_SECONDS,
  newAgentToolRef,
  type AgentToolRefSpec,
} from "../agentTools";
import type { AgentSpec } from "@/lib/api";
import { fieldClass, labelClass } from "./fieldStyles";
import { FieldProblem, groupClass, hintClass, legendClass, NumberField, problemClass, useShownOnce } from "./knowledgeMemoryFields";

/** Element id of a ref's field (`agent-tool-<index>-<field>`), matching `agentToolProblems` fields. */
export const agentToolFieldId = (field: string) => `agent-tool-${field}`;
const fieldId = agentToolFieldId;

/**
 * The Tools step's "Agents as tools" section (`spec.agent_tools`): published agents of this tenant the
 * model may call like tools. Each call runs the agent as a child run (its own trace, usage and
 * approvals); the result is its final answer. Per agent: tool name, description, version (latest or
 * pinned), timeout and the most calls per run.
 */
export function AgentToolsSection({ spec, onSpecChange }: { spec: AgentSpec; onSpecChange: (patch: Record<string, unknown>) => void }) {
  const rootRef = useRef<HTMLDivElement>(null);
  // The agent being edited (`/agents/[id]/editor`): it cannot call itself, so it is not offered.
  const selfId = (useParams() as { id?: string } | null)?.id;
  const refs = agentToolsOf(spec);
  // The agent list is loaded the first time the step is shown (or at once when the spec lists some,
  // so their names show).
  const shown = useShownOnce(rootRef, refs.length > 0);
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!shown) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => api.agents.list())
      .then((items) => {
        if (!cancelled) setAgents(items ?? []);
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : "Failed to load agents");
      });
    return () => {
      cancelled = true;
    };
  }, [shown]);

  const problems = agentToolProblems(spec);
  const problemFor = (field: string) => problems.find((p) => p.field === field)?.message ?? null;
  const write = (next: AgentToolRefSpec[]) => onSpecChange({ agent_tools: agentToolsValue(next) });
  const update = (index: number, patch: Partial<AgentToolRefSpec>) => write(refs.map((ref, i) => (i === index ? { ...ref, ...patch } : ref)));
  const byId = new Map((agents ?? []).map((agent) => [agent.id, agent]));
  const chosen = new Set(refs.map((ref) => ref.agent_id));
  // Only published, non-archived agents can be called; an agent already added is not offered again.
  const options = (agents ?? [])
    .filter((agent) => agent.latest_version != null && !agent.archived && !chosen.has(agent.id) && agent.id !== selfId)
    .map((agent) => ({ value: agent.id, label: agent.name, description: `Version ${agent.latest_version}` }));
  const full = refs.length >= MAX_AGENT_TOOLS_PER_AGENT;

  return (
    <section ref={rootRef} aria-labelledby="agent-tools-heading" data-testid="agent-tools-section" className="min-w-0 space-y-6">
      <h3 id="agent-tools-heading" className="text-label font-medium text-foreground">
        Agents as tools
      </h3>
      <p className="-mt-4 text-label text-muted-foreground">
        The model can call another published agent like a tool. Each call starts that agent as its own run, with its own tools, approvals, trace and cost, and
        returns its final answer. Use a workflow instead when the steps are fixed.
      </p>

      <div className="min-w-0">
        {loadError ? (
          <p role="alert" className={problemClass}>
            Could not load agents: {loadError}
          </p>
        ) : agents === null ? (
          <p role="status" className="text-caption text-muted-foreground">
            Loading agents…
          </p>
        ) : options.length === 0 && refs.length === 0 ? (
          <p data-testid="agent-tools-empty" className="text-label text-muted-foreground">
            No other published agents yet.{" "}
            <Link href={routes.agents()} className="text-foreground underline underline-offset-2">
              Publish an agent first
            </Link>
            .
          </p>
        ) : (
          <Combobox
            id={fieldId("list")}
            label="Add an agent"
            placeholder="Add an agent…"
            searchPlaceholder="Search published agents…"
            emptyText="No other published agents."
            options={options}
            value={null}
            disabled={full || options.length === 0}
            onValueChange={(id) => write([...refs, newAgentToolRef(id)])}
            className="border border-border bg-surface"
            testId="agent-tools-add"
          />
        )}
        <p className={hintClass} aria-live="polite" data-testid="agent-tools-count">
          {refs.length} added (max {MAX_AGENT_TOOLS_PER_AGENT})
        </p>
        {problemFor("list") ? <p className={problemClass}>{problemFor("list")}</p> : null}
      </div>

      {refs.map((ref, index) => {
        const agent = byId.get(ref.agent_id);
        const unknown = agents !== null && !agent;
        const titleId = `agent-tool-${index}-title`;
        const name = agent?.name ?? (agents === null ? ref.agent_id : "Unknown agent");
        const derived = defaultToolName(agent?.name, ref.agent_id);
        const pinned = ref.version !== undefined && ref.version !== "latest";
        return (
          <section key={`${ref.agent_id}-${index}`} aria-labelledby={titleId} data-testid="agent-tool-ref" className={groupClass}>
            <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <h3 id={titleId} className={`${legendClass} break-words px-0`}>
                  {name}
                </h3>
                <p className="break-all font-mono text-caption text-muted-foreground">{ref.agent_id}</p>
                {agent ? (
                  <Link href={routes.agentEditor(agent.id)} className="text-caption text-foreground underline underline-offset-2">
                    Open agent
                  </Link>
                ) : null}
              </div>
              <Button id={fieldId(`${index}-agent_id`)} size="sm" variant="ghost" onClick={() => write(refs.filter((_, i) => i !== index))} aria-label={`Remove ${name}`}>
                Remove
              </Button>
            </div>
            {unknown ? <p className={problemClass}>This agent does not exist in this tenant (deleted?). Publishing will fail until it is removed.</p> : null}
            {agent?.archived ? <p className={problemClass}>This agent is archived. Publishing will fail until it is removed or unarchived.</p> : null}
            {agent && agent.latest_version == null ? <p className={problemClass}>This agent has no published version. Publishing will fail until it is published.</p> : null}
            {problemFor(`${index}-agent_id`) ? <p className={problemClass}>{problemFor(`${index}-agent_id`)}</p> : null}

            <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
              <div className="min-w-0">
                <label htmlFor={fieldId(`${index}-name`)} className={labelClass}>
                  Tool name
                </label>
                <input
                  id={fieldId(`${index}-name`)}
                  type="text"
                  className={fieldClass}
                  value={ref.name ?? ""}
                  placeholder={derived}
                  spellCheck={false}
                  autoComplete="off"
                  aria-invalid={problemFor(`${index}-name`) ? true : undefined}
                  aria-describedby={`${fieldId(`${index}-name`)}-hint ${fieldId(`${index}-name`)}-problem`}
                  onChange={(event) => update(index, { name: event.target.value === "" ? undefined : event.target.value })}
                />
                <p id={`${fieldId(`${index}-name`)}-hint`} className={hintClass}>
                  What the model calls it. Empty: <code className="font-mono">{derived}</code>, from the agent&apos;s name.
                </p>
                <FieldProblem id={`${fieldId(`${index}-name`)}-problem`} message={problemFor(`${index}-name`)} />
              </div>
              <div className="min-w-0">
                <label htmlFor={fieldId(`${index}-version-mode`)} className={labelClass}>
                  Version
                </label>
                <Select
                  id={fieldId(`${index}-version-mode`)}
                  value={pinned ? "pinned" : "latest"}
                  onValueChange={(mode) => update(index, { version: mode === "pinned" ? (agent?.latest_version ?? 1) : undefined })}
                  className="w-full"
                >
                  <option value="latest">Latest published</option>
                  <option value="pinned">Pinned version</option>
                </Select>
                <p className={hintClass}>Latest is resolved when the tool is called.</p>
              </div>
            </div>
            {pinned ? (
              <NumberField
                id={fieldId(`${index}-version`)}
                label="Pinned version"
                value={ref.version}
                min={1}
                max={agent?.latest_version ?? 1_000_000}
                onChange={(value) => update(index, { version: value === undefined ? 1 : (value as number | string) })}
                problem={problemFor(`${index}-version`)}
              />
            ) : null}

            <div className="min-w-0">
              <label htmlFor={fieldId(`${index}-description`)} className={labelClass}>
                Description (optional)
              </label>
              <textarea
                id={fieldId(`${index}-description`)}
                rows={2}
                maxLength={MAX_DESCRIPTION_CHARS + 200}
                className={fieldClass}
                value={ref.description ?? ""}
                placeholder="What the model should use this agent for"
                aria-invalid={problemFor(`${index}-description`) ? true : undefined}
                onChange={(event) => update(index, { description: event.target.value === "" ? undefined : event.target.value })}
              />
              <p className={hintClass}>Empty: the model is told the agent&apos;s name and what its instructions say it does.</p>
              <FieldProblem id={`${fieldId(`${index}-description`)}-problem`} message={problemFor(`${index}-description`)} />
            </div>

            <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
              <NumberField
                id={fieldId(`${index}-timeout_seconds`)}
                label={`Timeout in seconds (1–${MAX_TIMEOUT_SECONDS})`}
                value={ref.timeout_seconds}
                min={1}
                max={MAX_TIMEOUT_SECONDS}
                placeholder={`${DEFAULT_TIMEOUT_SECONDS} (default)`}
                hint="The agent is cancelled after this long; the model reads an error."
                onChange={(value) => update(index, { timeout_seconds: value as number | string | undefined })}
                problem={problemFor(`${index}-timeout_seconds`)}
              />
              <NumberField
                id={fieldId(`${index}-max_calls_per_run`)}
                label={`Max calls per run (1–${MAX_CALLS_PER_RUN_LIMIT})`}
                value={ref.max_calls_per_run}
                min={1}
                max={MAX_CALLS_PER_RUN_LIMIT}
                placeholder={`${DEFAULT_MAX_CALLS_PER_RUN} (default)`}
                hint="More calls in one run are refused."
                onChange={(value) => update(index, { max_calls_per_run: value as number | string | undefined })}
                problem={problemFor(`${index}-max_calls_per_run`)}
              />
            </div>
          </section>
        );
      })}
    </section>
  );
}
