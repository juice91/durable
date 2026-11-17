"use client";

import React, { useId, useMemo, useState } from "react";
import Link from "next/link";
import { PublishWarnings } from "@/components/agents/PublishWarnings";
import { Button } from "@/components/ui/Button";
import { Switch } from "@/components/ui/Switch";
import {
  draftAgent,
  EMPTY_FORM,
  formToSpec,
  saveAgent,
  specToForm,
  ArchitectError,
  type AgentFormValues,
  type ArchitectAgentSpec,
  type ArchitectOptions,
  type DraftResult,
  type SaveResult,
} from "@/lib/architect";
import { routes } from "@/lib/routes";

const labelClass = "mb-1 block text-label font-medium text-foreground";
const fieldClass =
  "w-full min-w-0 rounded-md border border-input bg-surface px-3 py-2 text-body text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const hintClass = "mt-1 text-caption text-muted-foreground";
const checkboxClass = "mt-0.5 size-4 shrink-0 accent-[var(--primary)]";

/** What the page opens the form with: an existing created agent, or nothing for a new one. */
export interface ArchitectFormSource {
  spec: ArchitectAgentSpec | null;
  /** The latest published version of the agent being edited, sent as `expected_version`. */
  version: number | null;
  /** The agent's row id (links to its page), when editing one. */
  agentRowId: string | null;
}

export interface ArchitectFormProps {
  options: ArchitectOptions | null;
  source: ArchitectFormSource;
  /** After a successful save (draft or live). */
  onSaved: (result: SaveResult) => void;
  /** After a 409 version_mismatch, "Reload the latest" asks the page to reopen the agent. */
  onReload: () => void;
}

/** Keeps only what the options offer: the pickers are limited to them. */
function limitToOptions(spec: ArchitectAgentSpec, options: ArchitectOptions | null): AgentFormValues {
  const form = specToForm(spec);
  if (!options) return form;
  const tools = new Set(options.tools.map((tool) => tool.id));
  const kbs = new Set(options.knowledge_bases.map((kb) => kb.id));
  const mcp = new Set(options.mcp_servers.flatMap((server) => server.tools.map((tool) => `${server.name}\u0000${tool}`)));
  const models = new Set(options.models.map((model) => model.id));
  return {
    ...form,
    model: models.has(form.model) ? form.model : "",
    tools: form.tools.filter((id) => tools.has(id)),
    knowledge_bases: form.knowledge_bases.filter((id) => kbs.has(id)),
    mcp_tools: form.mcp_tools.filter((ref) => mcp.has(`${ref.server}\u0000${ref.tool}`)),
  };
}

function toggled<T>(list: readonly T[], item: T, on: boolean, same: (a: T, b: T) => boolean = Object.is): T[] {
  const rest = list.filter((existing) => !same(existing, item));
  return on ? [...rest, item] : rest;
}

type Status = { kind: "saved"; result: SaveResult } | null;

export function ArchitectForm({ options, source, onSaved, onReload }: ArchitectFormProps) {
  const ids = useId();
  const [form, setForm] = useState<AgentFormValues>(() => (source.spec ? limitToOptions(source.spec, options) : EMPTY_FORM));
  const [description, setDescription] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [assumptions, setAssumptions] = useState<string[]>([]);
  const [dropped, setDropped] = useState<DraftResult["dropped"]>([]);
  const [reviewed, setReviewed] = useState(false);
  const [version, setVersion] = useState<number | null>(source.version);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ message: string; staleVersion: number | null } | null>(null);
  const [status, setStatus] = useState<Status>(null);

  const set = <K extends keyof AgentFormValues>(key: K, value: AgentFormValues[K]) => setForm((current) => ({ ...current, [key]: value }));
  const needsReview = assumptions.length > 0 && !reviewed;
  const canSave = form.name.trim() !== "" && form.instructions.trim() !== "" && !saving;
  const approvalTools = useMemo(() => new Set((options?.tools ?? []).filter((tool) => tool.requires_approval).map((tool) => tool.id)), [options]);

  async function fillIn() {
    setDrafting(true);
    setDraftError(null);
    try {
      const result = await draftAgent(description, form.name.trim() && form.instructions.trim() ? formToSpec(form) : undefined);
      setForm(limitToOptions(result.spec, options));
      setAssumptions(result.assumptions);
      setDropped(result.dropped);
      setReviewed(false);
      setStatus(null);
      setSaveError(null);
    } catch (reason: unknown) {
      setDraftError(reason instanceof Error ? reason.message : "The draft failed");
    } finally {
      setDrafting(false);
    }
  }

  async function save(publish: boolean) {
    setSaving(true);
    setSaveError(null);
    setStatus(null);
    try {
      const result = await saveAgent(formToSpec(form), { publish, expectedVersion: version });
      setVersion(result.version);
      setStatus({ kind: "saved", result });
      setForm((current) => (current.id ? current : { ...current, id: result.id }));
      onSaved(result);
    } catch (reason: unknown) {
      if (reason instanceof ArchitectError && reason.code === "version_mismatch") {
        const latest = reason.extra.latest_version;
        setSaveError({ message: reason.message, staleVersion: typeof latest === "number" ? latest : 0 });
      } else {
        setSaveError({ message: reason instanceof Error ? reason.message : "The save failed", staleVersion: null });
      }
    } finally {
      setSaving(false);
    }
  }

  const id = (name: string) => `${ids}-${name}`;

  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="architect-form">
      <section aria-label="Describe the agent" className="space-y-2">
        <h2 className="text-title font-semibold">Describe it</h2>
        <label htmlFor={id("describe")} className="sr-only">Describe it</label>
        <textarea
          id={id("describe")}
          className={fieldClass}
          rows={3}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="What should this agent do, and for whom?"
        />
        <div className="flex items-center gap-3">
          <Button variant="primary" onClick={() => void fillIn()} disabled={drafting || description.trim() === ""}>
            {drafting ? "Drafting…" : "Fill in the form"}
          </Button>
          <span className="text-caption text-muted-foreground">The draft only fills the fields below; nothing is saved until you save.</span>
        </div>
        {draftError ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-label text-destructive-text">{draftError}</div>
        ) : null}
      </section>

      {assumptions.length > 0 ? (
        <section data-testid="architect-assumptions-block" className="space-y-2 rounded-md border border-border bg-muted/40 p-3">
          <h3 className="text-label font-medium">Assumptions the draft made</h3>
          <ul data-testid="architect-assumptions" className="list-inside list-disc space-y-0.5 break-words text-label">
            {assumptions.map((assumption) => (
              <li key={assumption}>{assumption}</li>
            ))}
          </ul>
          <label className="flex items-center gap-2 text-label">
            <input type="checkbox" className={checkboxClass} checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} />
            I have reviewed these assumptions
          </label>
        </section>
      ) : null}

      {dropped.length > 0 ? (
        <p data-testid="architect-dropped" className="rounded-md border border-warning/40 bg-warning/10 p-3 text-label text-warning-text">
          Left out because they are not available here: {dropped.map((item) => `${item.kind} ${item.id}`).join(", ")}.
        </p>
      ) : null}

      <section aria-labelledby={id("form-title")} className="space-y-4">
        <h2 id={id("form-title")} className="text-title font-semibold">Fill in the form</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor={id("name")} className={labelClass}>Name</label>
            <input id={id("name")} className={fieldClass} value={form.name} maxLength={80} onChange={(event) => set("name", event.target.value)} />
          </div>
          <div>
            <label htmlFor={id("id")} className={labelClass}>Id</label>
            <input id={id("id")} className={`${fieldClass} font-mono`} value={form.id} disabled={source.agentRowId !== null} onChange={(event) => set("id", event.target.value)} aria-describedby={id("id-hint")} />
            <p id={id("id-hint")} className={hintClass}>Lowercase letters, digits and hyphens. Left empty, it is made from the name.</p>
          </div>
        </div>
        <div>
          <label htmlFor={id("description")} className={labelClass}>Description</label>
          <input id={id("description")} className={fieldClass} value={form.description} maxLength={300} onChange={(event) => set("description", event.target.value)} />
        </div>
        <div>
          <label htmlFor={id("instructions")} className={labelClass}>Instructions</label>
          <textarea id={id("instructions")} className={fieldClass} rows={8} maxLength={8000} value={form.instructions} onChange={(event) => set("instructions", event.target.value)} />
        </div>
        <div>
          <label htmlFor={id("model")} className={labelClass}>Model</label>
          <select id={id("model")} className={fieldClass} value={form.model} onChange={(event) => set("model", event.target.value)}>
            <option value="">{options?.default_model ? `Default (${options.default_model})` : "Default"}</option>
            {(options?.models ?? []).map((model) => (
              <option key={model.id} value={model.id}>{model.id}</option>
            ))}
          </select>
        </div>

        <fieldset className="min-w-0">
          <legend className={labelClass}>Tools</legend>
          {(options?.tools ?? []).length === 0 ? <p className={hintClass}>No tools are available.</p> : null}
          <ul className="space-y-2">
            {(options?.tools ?? []).map((tool) => (
              <li key={tool.id}>
                <label className="flex items-start gap-2 text-label">
                  <input type="checkbox" className={checkboxClass} checked={form.tools.includes(tool.id)} onChange={(event) => set("tools", toggled(form.tools, tool.id, event.target.checked))} />
                  <span className="min-w-0">
                    <span className="font-medium">{tool.title}</span>
                    {approvalTools.has(tool.id) ? (
                      <span data-testid="architect-needs-approval" className="ms-2 rounded-sm bg-warning/10 px-1.5 py-0.5 text-badge text-warning-text">needs approval</span>
                    ) : null}
                    <span className="block text-caption text-muted-foreground">{tool.description}</span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
        </fieldset>

        {(options?.mcp_servers ?? []).length > 0 ? (
          <fieldset className="min-w-0">
            <legend className={labelClass}>MCP tools</legend>
            <div className="space-y-3">
              {(options?.mcp_servers ?? []).map((server) => (
                <div key={server.id}>
                  <p className="text-caption font-medium text-muted-foreground">{server.name}</p>
                  <ul className="space-y-1">
                    {server.tools.map((tool) => (
                      <li key={tool}>
                        <label className="flex items-center gap-2 text-label">
                          <input
                            type="checkbox"
                            className={checkboxClass}
                            checked={form.mcp_tools.some((ref) => ref.server === server.name && ref.tool === tool)}
                            onChange={(event) =>
                              set("mcp_tools", toggled(form.mcp_tools, { server: server.name, tool }, event.target.checked, (a, b) => a.server === b.server && a.tool === b.tool))
                            }
                          />
                          <span>{server.name} · {tool}</span>
                        </label>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </fieldset>
        ) : null}

        {(options?.knowledge_bases ?? []).length > 0 ? (
          <fieldset className="min-w-0">
            <legend className={labelClass}>Knowledge bases</legend>
            <ul className="space-y-1">
              {(options?.knowledge_bases ?? []).map((kb) => (
                <li key={kb.id}>
                  <label className="flex items-center gap-2 text-label">
                    <input type="checkbox" className={checkboxClass} checked={form.knowledge_bases.includes(kb.id)} onChange={(event) => set("knowledge_bases", toggled(form.knowledge_bases, kb.id, event.target.checked))} />
                    <span>{kb.name}</span>
                  </label>
                </li>
              ))}
            </ul>
          </fieldset>
        ) : null}

        <div className="flex items-center gap-3">
          <Switch id={id("memory")} checked={form.memory} onCheckedChange={(on) => set("memory", on)} />
          <label htmlFor={id("memory")} className="text-label font-medium">Memory</label>
          <span className="text-caption text-muted-foreground">Keeps notes about each user between conversations.</span>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor={id("temperature")} className={labelClass}>Temperature</label>
            <input id={id("temperature")} className={fieldClass} inputMode="decimal" value={form.temperature} onChange={(event) => set("temperature", event.target.value)} />
          </div>
          <div>
            <label htmlFor={id("max-tokens")} className={labelClass}>Max tokens</label>
            <input id={id("max-tokens")} className={fieldClass} inputMode="numeric" value={form.max_tokens} onChange={(event) => set("max_tokens", event.target.value)} />
          </div>
        </div>
      </section>

      {saveError?.staleVersion != null ? (
        <div role="alert" data-testid="architect-version-mismatch" className="space-y-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-label text-warning-text">
          <p>
            This agent changed since you opened it{saveError.staleVersion > 0 ? ` (it is now at v${saveError.staleVersion})` : ""}. Nothing was saved, so your edits here are still in the form.
          </p>
          <Button onClick={onReload}>Reload the latest</Button>
        </div>
      ) : saveError ? (
        <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-label text-destructive-text">{saveError.message}</div>
      ) : null}

      {status ? (
        <div className="space-y-2" data-testid="architect-saved">
          <p className="text-label">
            {status.result.published ? `Live as v${status.result.version}.` : `Saved as a draft (v${status.result.version} stays what it was).`}{" "}
            <Link className="underline underline-offset-4" href={routes.agentIndex(status.result.agent_id)}>Open the agent</Link>
          </p>
          <PublishWarnings warnings={status.result.warnings} />
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => void save(false)} disabled={!canSave}>Save draft</Button>
        <Button variant="primary" onClick={() => void save(true)} disabled={!canSave || needsReview}>Save and go live</Button>
        {needsReview ? <span className="text-caption text-muted-foreground">Review the assumptions to go live.</span> : null}
      </div>
    </div>
  );
}
