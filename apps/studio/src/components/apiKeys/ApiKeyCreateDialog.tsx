"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { api } from "@/lib/api";
import { apiKeys, buildApiKeyCreate, withoutSecret, type ApiKey, type ApiKeyCreated } from "@/lib/apiKeys";
import { localInputToIso } from "@/lib/datetimeLocal";
import { listWorkflows } from "@/lib/workflows";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { CopyField } from "./CopyField";

const fieldClass =
  "w-full min-w-0 rounded-md border border-input bg-surface px-3 py-1.5 text-label text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const labelClass = "block text-label font-medium text-foreground";
const hintClass = "text-caption text-muted-foreground";
const alertClass = "rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text";

interface Option {
  id: string;
  name: string;
}

type Options = { status: "idle" | "loading" } | { status: "ready"; agents: Option[]; workflows: Option[] } | { status: "error"; message: string };

function useTargets(enabled: boolean): Options & { retry: () => void } {
  const [options, setOptions] = useState<Options>({ status: "idle" });
  // One load per attempt, the first time `enabled` is true; Retry starts the next attempt. Not
  // cancelled by its own state change: only an unmount (or a newer attempt) drops an answer.
  const [attempt, setAttempt] = useState(0);
  const startedAttempt = useRef(-1);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (!enabled || startedAttempt.current === attempt) return;
    startedAttempt.current = attempt;
    const current = attempt;
    const live = () => alive.current && startedAttempt.current === current;
    setOptions({ status: "loading" });
    Promise.all([api.agents.list(), listWorkflows()])
      .then(([agents, workflows]) => {
        if (!live()) return;
        const byName = (a: Option, b: Option) => a.name.localeCompare(b.name);
        setOptions({
          status: "ready",
          agents: agents.map((a) => ({ id: a.id, name: a.name })).sort(byName),
          workflows: workflows.map((w) => ({ id: w.id, name: w.name })).sort(byName),
        });
      })
      .catch((reason: unknown) => {
        if (live()) setOptions({ status: "error", message: reason instanceof Error ? reason.message : "Could not load agents and workflows" });
      });
  }, [enabled, attempt]);
  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { ...options, retry } as Options & { retry: () => void };
}

function Checklist({ legend, items, selected, onChange, testId }: { legend: string; items: Option[]; selected: string[]; onChange: (ids: string[]) => void; testId: string }) {
  return (
    <fieldset className="min-w-0" data-testid={testId}>
      <legend className={labelClass}>{legend}</legend>
      {items.length === 0 ? (
        <p className={hintClass}>None yet.</p>
      ) : (
        <ul className="mt-1 max-h-40 min-w-0 overflow-y-auto rounded-md border border-border p-2">
          {items.map((item) => (
            <li key={item.id} className="min-w-0">
              <label className="flex min-w-0 items-center gap-2 py-0.5 text-label text-foreground">
                <input
                  type="checkbox"
                  checked={selected.includes(item.id)}
                  onChange={(event) => onChange(event.target.checked ? [...selected, item.id] : selected.filter((id) => id !== item.id))}
                />
                <span className="truncate">{item.name}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </fieldset>
  );
}

interface ApiKeyCreateDialogProps {
  /** Called with the new key's listed fields (never its plaintext) as soon as it is created. */
  onCreated: (key: ApiKey) => void;
  onClose: () => void;
}

/**
 * "Create API key": a name, what it may run, allowed origins and an expiry. Once created, the
 * dialog shows the plaintext key this one time with a Copy button; closing the dialog drops it, and
 * nothing else on the page (or a reload) can show it again. Mounted while open.
 */
export function ApiKeyCreateDialog({ onCreated, onClose }: ApiKeyCreateDialogProps) {
  const id = useId();
  const [name, setName] = useState("");
  const [access, setAccess] = useState<"all" | "some">("all");
  const [agentIds, setAgentIds] = useState<string[]>([]);
  const [workflowIds, setWorkflowIds] = useState<string[]>([]);
  const [origins, setOrigins] = useState("");
  const [expires, setExpires] = useState("");
  const [mcp, setMcp] = useState(false);
  const [manage, setManage] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Only here, only while the dialog is open.
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const targets = useTargets(access === "some");
  // The form (and its focused submit button) is replaced by the secret view: move focus to its Copy
  // button so it stays inside the dialog instead of falling to <body>.
  const secretRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (created) secretRef.current?.querySelector<HTMLElement>("[data-copy-button]")?.focus();
  }, [created]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    const expiresAt = expires ? localInputToIso(expires) : null;
    if (expires && !expiresAt) {
      setError("The expiry is not a valid date and time.");
      return;
    }
    const built = buildApiKeyCreate({ name, access, agentIds, workflowIds, origins, expiresAt, mcp: mcp || manage, manage });
    if (!built.ok) {
      setError(built.message);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await apiKeys.create(built.body);
      setCreated(result);
      onCreated(withoutSecret(result));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "The key could not be created");
    } finally {
      setSaving(false);
    }
  };

  const close = () => {
    setCreated(null);
    onClose();
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !saving) close(); }}>
      <DialogContent className="md:max-w-xl" data-testid="api-key-create-dialog">
        {created ? (
          <>
            <DialogHeader>
              <DialogTitle>Copy your new key</DialogTitle>
              <DialogDescription>
                This is the only time the key is shown. Store it in your backend&apos;s secrets now; if you lose it, revoke it and create another.
              </DialogDescription>
            </DialogHeader>
            <div ref={secretRef} data-testid="api-key-secret">
              <CopyField label={`${created.name} key`} value={created.key} copiedMessage="Key copied" />
            </div>
            <DialogFooter>
              <Button variant="primary" onClick={close} data-testid="api-key-done">
                Done
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Create API key</DialogTitle>
              <DialogDescription>
                A key lets a third-party frontend run agents and workflows over AG-UI, and an MCP client read this workspace. It cannot run drafts or change the model.
              </DialogDescription>
            </DialogHeader>
            <form onSubmit={(event) => void submit(event)} className="min-w-0 space-y-4" aria-label="Create API key" data-testid="api-key-create-form">
              <div className="space-y-1">
                <label htmlFor={`${id}-name`} className={labelClass}>Name</label>
                <input
                  id={`${id}-name`}
                  data-testid="api-key-name"
                  value={name}
                  maxLength={100}
                  required
                  onChange={(event) => setName(event.target.value)}
                  className={fieldClass}
                  placeholder="Support site"
                />
              </div>
              <fieldset className="min-w-0 space-y-1">
                <legend className={labelClass}>Access</legend>
                <label className="flex items-center gap-2 text-label text-foreground">
                  <input type="radio" name={`${id}-access`} checked={access === "all"} onChange={() => setAccess("all")} data-testid="api-key-access-all" />
                  All agents and workflows
                </label>
                <label className="flex items-center gap-2 text-label text-foreground">
                  <input type="radio" name={`${id}-access`} checked={access === "some"} onChange={() => setAccess("some")} data-testid="api-key-access-some" />
                  Only the ones I pick
                </label>
              </fieldset>
              {access === "some" ? (
                targets.status === "ready" ? (
                  <div className="grid min-w-0 gap-3 md:grid-cols-2">
                    <Checklist legend="Agents" items={targets.agents} selected={agentIds} onChange={setAgentIds} testId="api-key-agents" />
                    <Checklist legend="Workflows" items={targets.workflows} selected={workflowIds} onChange={setWorkflowIds} testId="api-key-workflows" />
                  </div>
                ) : targets.status === "error" ? (
                  <div className="flex min-w-0 flex-col items-start gap-2">
                    <p role="alert" className={alertClass}>{targets.message}</p>
                    <Button type="button" variant="secondary" size="sm" onClick={targets.retry} data-testid="api-key-targets-retry">
                      Retry
                    </Button>
                  </div>
                ) : (
                  <p className={hintClass}>Loading agents and workflows…</p>
                )
              ) : null}
              <div className="space-y-1">
                <label htmlFor={`${id}-origins`} className={labelClass}>Allowed origins</label>
                <textarea
                  id={`${id}-origins`}
                  data-testid="api-key-origins"
                  value={origins}
                  rows={2}
                  onChange={(event) => setOrigins(event.target.value)}
                  className={fieldClass}
                  placeholder="https://app.example.com"
                  aria-describedby={`${id}-origins-hint`}
                />
                <p id={`${id}-origins-hint`} className={hintClass}>
                  One per line. Leave empty for server-side use; a browser sending another Origin is refused.
                </p>
              </div>
              <div className="space-y-1">
                <label htmlFor={`${id}-expires`} className={labelClass}>Expires</label>
                <input
                  id={`${id}-expires`}
                  data-testid="api-key-expires"
                  type="datetime-local"
                  value={expires}
                  onChange={(event) => setExpires(event.target.value)}
                  className={fieldClass}
                  aria-describedby={`${id}-expires-hint`}
                />
                <p id={`${id}-expires-hint`} className={hintClass}>Optional, in your local time.</p>
              </div>
              <fieldset className="min-w-0 space-y-2">
                <legend className={labelClass}>MCP access</legend>
                <label className="flex items-center gap-2 text-label text-foreground">
                  <input type="checkbox" checked={mcp || manage} disabled={manage} onChange={(event) => setMcp(event.target.checked)} data-testid="api-key-mcp-scope" aria-describedby={`${id}-mcp-hint`} />
                  Connect over MCP (read)
                </label>
                <p id={`${id}-mcp-hint`} className={hintClass}>
                  Lets an MCP client using this key read agents, workflows, runs and knowledge bases. A key without it is refused at the MCP endpoint.
                </p>
                <label className="flex items-center gap-2 text-label text-foreground">
                  <input type="checkbox" checked={manage} onChange={(event) => setManage(event.target.checked)} data-testid="api-key-manage-scope" aria-describedby={`${id}-manage-hint`} />
                  Allow changes (MCP)
                </label>
                <p id={`${id}-manage-hint`} className={hintClass}>
                  Also lets it publish and archive agents and workflows, start and cancel runs, sync knowledge sources and decide approvals. Includes read access.
                </p>
              </fieldset>
              {error ? (
                <p role="alert" className={alertClass} data-testid="api-key-create-error">
                  {error}
                </p>
              ) : null}
              <DialogFooter>
                <Button type="button" variant="secondary" onClick={close} disabled={saving}>
                  Cancel
                </Button>
                <Button type="submit" variant="primary" disabled={saving} data-testid="api-key-create-submit">
                  {saving ? "Creating…" : "Create key"}
                </Button>
              </DialogFooter>
            </form>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
