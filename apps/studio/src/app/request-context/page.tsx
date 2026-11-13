"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { api, type Agent } from "@/lib/api";
import {
  IDLE_VALIDATION,
  RequestContextForm,
  type ContextValidationState,
} from "@/components/requestContext/RequestContextForm";
import {
  clearSavedContext,
  getRequestContext,
  loadSavedContext,
  playgroundHref,
  saveContext,
  type ContextVersionRef,
  type RequestContextInfo,
  type RequestContextValue,
  type SavedRequestContext,
} from "@/lib/requestContext";
import { routes } from "@/lib/routes";

type VersionChoice = number | "draft";

const selectClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const STORAGE_UNAVAILABLE = "Browser storage is unavailable, so the context cannot be saved here.";

function versionLabel(choice: VersionChoice, latest: number | null): string {
  if (choice === "draft") return "Draft";
  return choice === latest ? `v${choice} (latest)` : `v${choice}`;
}

/** The saved version when it still exists, else the latest published version, else the draft. */
function defaultVersion(agent: Agent, saved: SavedRequestContext | null): VersionChoice {
  const latest = agent.latest_version;
  const wanted = saved?.version;
  if (wanted === "draft") return "draft";
  if (typeof wanted === "number" && latest !== null && wanted <= latest) return wanted;
  return latest ?? "draft";
}

function parseVersionParam(raw: string | null, agent: Agent): VersionChoice | null {
  if (raw === "draft") return "draft";
  if (raw && /^\d+$/.test(raw)) {
    const number = Number(raw);
    if (number >= 1 && agent.latest_version !== null && number <= agent.latest_version) return number;
  }
  return null;
}

function replaceQuery(agentId: string, version: VersionChoice) {
  if (typeof window === "undefined") return;
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("agent", agentId);
    url.searchParams.set("version", String(version));
    window.history.replaceState(window.history.state, "", url.toString());
  } catch {
    // The URL is a convenience only.
  }
}

export default function RequestContextPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [agentsError, setAgentsError] = useState<string | null>(null);
  const [agentId, setAgentId] = useState("");
  const [version, setVersion] = useState<VersionChoice>("draft");
  const [info, setInfo] = useState<RequestContextInfo | null>(null);
  const [infoLoading, setInfoLoading] = useState(false);
  const [infoError, setInfoError] = useState<string | null>(null);
  const [value, setValue] = useState<RequestContextValue>({});
  const [validation, setValidation] = useState<ContextValidationState>(IDLE_VALIDATION);
  const [saved, setSaved] = useState<SavedRequestContext | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);

  const agent = agents.find((item) => item.id === agentId) ?? null;

  const selectAgent = useCallback((next: Agent, versionOverride: VersionChoice | null = null) => {
    const stored = loadSavedContext(next.id);
    const choice = versionOverride ?? defaultVersion(next, stored);
    setAgentId(next.id);
    setVersion(choice);
    setSaved(stored);
    setValue(stored?.value ?? {});
    setNotice(null);
    replaceQuery(next.id, choice);
  }, []);

  useEffect(() => {
    let cancelled = false;
    api.agents.list()
      .then((items) => {
        if (cancelled) return;
        setAgents(items);
        const params = typeof window === "undefined" ? null : new URLSearchParams(window.location.search);
        const fromUrl = items.find((item) => item.id === params?.get("agent"));
        const first = fromUrl ?? items[0];
        if (first) selectAgent(first, fromUrl ? parseVersionParam(params?.get("version") ?? null, fromUrl) : null);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setAgentsError(reason instanceof Error ? reason.message : "Failed to load agents");
      })
      .finally(() => {
        if (!cancelled) setAgentsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectAgent]);

  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    setInfo(null);
    setInfoError(null);
    setInfoLoading(true);
    setValidation(IDLE_VALIDATION);
    getRequestContext(agentId, version)
      .then((result) => {
        if (!cancelled) setInfo(result);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setInfoError(reason instanceof Error ? reason.message : "Failed to load the context fields");
      })
      .finally(() => {
        if (!cancelled) setInfoLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, version]);

  const versionChoices = useMemo<VersionChoice[]>(() => {
    if (!agent) return [];
    const published = agent.latest_version === null
      ? []
      : Array.from({ length: agent.latest_version }, (_, index) => agent.latest_version! - index);
    return [...published, "draft"];
  }, [agent]);

  const changeVersion = (next: VersionChoice) => {
    setVersion(next);
    setNotice(null);
    replaceQuery(agentId, next);
  };

  const versionRef: ContextVersionRef = version;
  const canSave = validation.status === "valid" && validation.accepted !== null;

  const save = (): boolean => {
    if (!canSave || !agentId) return false;
    const result = saveContext(agentId, validation.accepted!, versionRef);
    if (!result) {
      setNotice({ tone: "warn", text: STORAGE_UNAVAILABLE });
      return false;
    }
    setSaved(result);
    setNotice({ tone: "ok", text: "Saved. The playground preselects this context for this agent." });
    return true;
  };

  const clear = () => {
    if (!clearSavedContext(agentId)) {
      setNotice({ tone: "warn", text: STORAGE_UNAVAILABLE });
      return;
    }
    setSaved(null);
    setNotice({ tone: "ok", text: "Saved context removed." });
  };

  return (
    <div className="mx-auto max-w-7xl px-4 py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-foreground">Request Context</h1>
        <p className="mt-1 text-muted-foreground">
          Typed values an agent receives per run. Fill them for a version, check them against the API, and save them for the playground.
        </p>
      </div>

      {agentsError ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{agentsError}</p> : null}
      {agentsLoading ? <p className="text-muted-foreground">Loading agents…</p> : null}
      {!agentsLoading && !agentsError && agents.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-8 text-center">
          <p className="font-medium text-foreground">No agents yet</p>
          <p className="mt-1 text-sm text-muted-foreground">Request context is declared on an agent.</p>
          <Link href={routes.agentNew()} className="mt-3 inline-block text-sm text-primary underline-offset-4 hover:underline">Create an agent</Link>
        </div>
      ) : null}

      {agent ? (
        <div className="grid gap-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
          <aside className="space-y-4">
            <section className="space-y-3 rounded-lg border border-border bg-card p-4">
              <div className="space-y-1">
                <label htmlFor="request-context-agent" className="block text-sm font-medium text-foreground">Agent</label>
                <select
                  id="request-context-agent"
                  className={selectClass}
                  value={agentId}
                  onChange={(event) => {
                    const next = agents.find((item) => item.id === event.target.value);
                    if (next) selectAgent(next);
                  }}
                >
                  {agents.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select>
              </div>
              <div className="space-y-1">
                <label htmlFor="request-context-version" className="block text-sm font-medium text-foreground">Version</label>
                <select
                  id="request-context-version"
                  className={selectClass}
                  value={String(version)}
                  onChange={(event) => changeVersion(event.target.value === "draft" ? "draft" : Number(event.target.value))}
                >
                  {versionChoices.map((choice) => <option key={String(choice)} value={String(choice)}>{versionLabel(choice, agent.latest_version)}</option>)}
                </select>
                {agent.latest_version === null ? <p className="text-xs text-muted-foreground">Not published yet: only the draft is available.</p> : null}
              </div>
              <Link href={routes.agentEditor(agent.id)} className="inline-block text-sm text-primary underline-offset-4 hover:underline">Edit fields in the agent editor</Link>
            </section>

            <section className="space-y-2 rounded-lg border border-border bg-card p-4" data-testid="saved-context">
              <h2 className="text-sm font-semibold text-card-foreground">Saved for the playground</h2>
              {saved ? (
                <>
                  <p className="text-xs text-muted-foreground">
                    {saved.version === null ? "Latest version" : versionLabel(saved.version, agent.latest_version)}
                    {saved.saved_at ? ` · saved ${new Date(saved.saved_at).toLocaleString()}` : ""}
                  </p>
                  <pre className="max-h-48 overflow-auto rounded-md bg-muted p-3 text-xs text-foreground">{JSON.stringify(saved.value, null, 2)}</pre>
                  <button type="button" onClick={clear} className="text-xs text-muted-foreground hover:text-foreground">Remove saved context</button>
                </>
              ) : <p className="text-xs text-muted-foreground">Nothing saved for this agent in this browser.</p>}
              <p data-testid="saved-context-storage-note" className="text-xs text-muted-foreground">
                Saved values are stored unencrypted in this browser&apos;s local storage. Don&apos;t save secrets.
              </p>
            </section>
          </aside>

          <main className="min-w-0 space-y-6">
            {infoError ? <p role="alert" data-testid="context-load-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{infoError}</p> : null}
            {infoLoading ? <p className="text-muted-foreground">Loading context fields…</p> : null}

            {info && info.fields.length === 0 ? (
              <section data-testid="context-no-fields" className="rounded-lg border border-dashed border-border p-8 text-center">
                <p className="font-medium text-foreground">{version === "draft" ? "The draft" : `Version ${version}`} declares no context fields</p>
                <p className="mt-1 text-sm text-muted-foreground">
                  Add typed fields to the agent, then use them in the instructions as <code className="font-mono">{"{{context.<name>}}"}</code> or read them from tools.
                </p>
                <Link href={routes.agentEditor(agent.id)} className="mt-3 inline-block text-sm text-primary underline-offset-4 hover:underline">Open the agent editor</Link>
              </section>
            ) : null}

            {info && info.fields.length > 0 ? (
                <section className="rounded-lg border border-border bg-card p-5">
                  <h2 className="mb-3 font-semibold text-card-foreground">Fields</h2>
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm" aria-label="Context fields">
                      <thead className="text-xs uppercase text-muted-foreground">
                        <tr>
                          <th className="py-2 pr-4 font-medium">Name</th>
                          <th className="py-2 pr-4 font-medium">Type</th>
                          <th className="py-2 pr-4 font-medium">Required</th>
                          <th className="py-2 pr-4 font-medium">Constraints</th>
                          <th className="py-2 font-medium">Description</th>
                        </tr>
                      </thead>
                      <tbody>
                        {info.fields.map((field) => (
                          <tr key={field.name} className="border-t border-border align-top">
                            <td className="py-2 pr-4 font-mono text-foreground">{field.name}</td>
                            <td className="py-2 pr-4 text-foreground">{field.type}</td>
                            <td className="py-2 pr-4 text-foreground">{field.required ? "Yes" : "No"}</td>
                            <td className="py-2 pr-4 text-muted-foreground">
                              {field.type === "enum" ? field.values.join(", ") : field.max_length !== null && field.max_length !== undefined ? `≤ ${field.max_length} chars` : "—"}
                            </td>
                            <td className="py-2 text-muted-foreground">{field.description || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <details className="mt-4">
                    <summary className="cursor-pointer text-sm text-muted-foreground hover:text-foreground">JSON schema</summary>
                    <pre data-testid="context-json-schema" className="mt-2 overflow-x-auto rounded-md bg-muted p-4 text-xs text-foreground">{JSON.stringify(info.schema, null, 2)}</pre>
                  </details>
                </section>
            ) : null}

            {/* With no fields the Values section still appears while the value has keys (e.g. a
                saved context from another version), so they can be seen and removed. */}
            {info && (info.fields.length > 0 || Object.keys(value).length > 0) ? (
                <section className="rounded-lg border border-border bg-card p-5">
                  <h2 className="mb-4 font-semibold text-card-foreground">Values</h2>
                  <RequestContextForm
                    key={`${agentId}:${String(version)}`}
                    agentId={agentId}
                    version={versionRef}
                    schema={info.schema}
                    value={value}
                    onChange={(next) => {
                      setValue(next);
                      setNotice(null);
                    }}
                    onValidation={setValidation}
                  />
                  <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-border pt-4">
                    <button
                      type="button"
                      onClick={save}
                      disabled={!canSave}
                      className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Save for playground
                    </button>
                    <Link
                      href={playgroundHref(agentId, canSave ? versionRef : (saved?.version ?? versionRef), true)}
                      onClick={() => {
                        if (canSave) save();
                      }}
                      className="rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground hover:bg-accent"
                    >
                      Use in playground
                    </Link>
                    {!canSave && validation.status !== "checking" ? <span className="text-xs text-muted-foreground">Only a valid context can be saved.</span> : null}
                  </div>
                  {notice ? (
                    <p role="status" data-testid="context-save-notice" className={`mt-3 text-sm ${notice.tone === "warn" ? "text-destructive-text" : "text-muted-foreground"}`}>{notice.text}</p>
                  ) : null}
                </section>
            ) : null}
          </main>
        </div>
      ) : null}
    </div>
  );
}
