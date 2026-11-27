"use client";

import React, { useCallback, useEffect, useId, useState } from "react";
import { Plus } from "lucide-react";
import { PageHeader } from "@/components/frame/PageHeader";
import { CopyField } from "@/components/apiKeys/CopyField";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { EmptyState } from "@/components/shared/EmptyState";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import {
  DISABLED_REASONS,
  expiryAfter,
  externalAccess,
  type ExportInput,
  type ExternalClient,
  type ExternalSettings,
  type Protocol,
  type ResultMode,
  type WorkflowExport,
} from "@/lib/externalAccess";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { navCrumb } from "@/lib/nav";
import { Can, useCan } from "@/lib/permissions";
import { listWorkflows } from "@/lib/workflows";
import { A2AEndpointSection } from "./A2AEndpointSection";
import { alertClass, fieldClass, hintClass, labelClass, message } from "./styles";

const warningClass = "rounded-md border border-warning/30 bg-warning/10 p-2 text-caption text-foreground";
const rowButtonClass = "min-w-0 break-words text-left text-label font-medium text-foreground underline-offset-2 hover:underline";
const KEY_DAYS = 90;

function when(iso: string | null): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString();
}

interface WorkflowOption {
  id: string;
  name: string;
}

const StatusBadge = ({ enabled, testId }: { enabled: boolean; testId?: string }) => (
  <Badge variant={enabled ? "success" : "neutral"} data-testid={testId}>{enabled ? "Enabled" : "Disabled"}</Badge>
);

/** What stands between the platform and an external agent, in the API's own terms. */
function PlatformStatus({ settings }: { settings: ExternalSettings }) {
  const missing = [
    settings.enabled ? null : "External access is off (EAP_EXTERNAL_ACCESS_ENABLED): no external agent can start anything.",
    settings.base_url ? null : "EAP_EXTERNAL_BASE_URL is not set: exports can be drafted, not enabled.",
    settings.grants_enforced ? null : "Agent and workflow grants are not enforced (EAP_AGENT_GRANTS_ENFORCE): exports cannot be enabled.",
  ].filter((m): m is string => m !== null);
  return (
    <div className="space-y-1" data-testid="external-status">
      {missing.length === 0 ? <p className={hintClass}>External access is on.</p> : missing.map((m) => <p key={m} className={warningClass}>{m}</p>)}
      {settings.base_url ? (
        <p className={hintClass}>MCP endpoint for external agents: <code className="break-all">{`${settings.base_url}/api/v1/external/mcp`}</code></p>
      ) : null}
    </div>
  );
}

function CreateClientDialog({ onCreated, onClose }: { onCreated: (client: ExternalClient) => void; onClose: () => void }) {
  const id = useId();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      onCreated(await externalAccess.createClient({ name, description }));
      onClose();
    } catch (reason: unknown) {
      setError(message(reason, "The client could not be created"));
      setSaving(false);
    }
  };
  return (
    <Dialog open onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New external client</DialogTitle>
          <DialogDescription>One external agent. It owns the runs it starts, so its keys can be rotated without losing them.</DialogDescription>
        </DialogHeader>
        <form onSubmit={(event) => void submit(event)} className="min-w-0 space-y-4" aria-label="New external client">
          <div className="space-y-1">
            <label htmlFor={`${id}-name`} className={labelClass}>Name</label>
            <input id={`${id}-name`} data-testid="external-client-name" value={name} maxLength={100} required onChange={(e) => setName(e.target.value)} className={fieldClass} placeholder="claude-code" />
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-description`} className={labelClass}>Description</label>
            <input id={`${id}-description`} value={description} maxLength={500} onChange={(e) => setDescription(e.target.value)} className={fieldClass} />
          </div>
          {error ? <p role="alert" className={alertClass}>{error}</p> : null}
          <DialogFooter>
            <Button variant="secondary" type="button" onClick={onClose} disabled={saving}>Cancel</Button>
            <Button variant="primary" type="submit" disabled={saving || !name.trim()} data-testid="external-client-submit">Create client</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ClientSheet({ client, workflows, onReload, onClose }: { client: ExternalClient; workflows: WorkflowOption[]; onReload: () => Promise<void>; onClose: () => void }) {
  const id = useId();
  const canManage = useCan("tenant", null, "manage_external_access");
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [days, setDays] = useState(KEY_DAYS);
  // The new key: only here, only while this sheet is open.
  const [secret, setSecret] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"cancel" | { revoke: string } | null>(null);
  const nameOf = (workflowId: string) => workflows.find((w) => w.id === workflowId)?.name ?? workflowId;

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      await onReload();
    } catch (reason: unknown) {
      setError(message(reason, "The change was refused"));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const createKey = (event: React.FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const made = await externalAccess.createKey(client.id, { name, workflow_ids: chosen, expires_at: expiryAfter(days) });
      setSecret(made.key);
      setName("");
      setChosen([]);
    });
  };

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent size="wide" data-testid="external-client-detail">
        <SheetHeader title={client.name} description={client.description || "External client"} />
        <SheetBody className="space-y-5">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge enabled={client.enabled} testId="external-client-status" />
            {canManage ? (
              <Button variant="secondary" size="sm" disabled={busy} data-testid="external-client-toggle" onClick={() => void run(async () => { await externalAccess.updateClient(client.id, { enabled: !client.enabled }, client.revision); })}>
                {client.enabled ? "Disable client" : "Enable client"}
              </Button>
            ) : null}
          </div>
          <p className={hintClass}>Disabling a client stops its keys at the next request. Runs already started go on until you cancel them.</p>
          {error ? <p role="alert" className={alertClass}>{error}</p> : null}

          <section aria-label="Keys" className="space-y-2">
            <h3 className="text-label font-medium text-foreground">Keys</h3>
            {client.keys.length === 0 ? <p className={hintClass}>No key yet.</p> : (
              <ul className="divide-y divide-border" data-testid="external-keys">
                {client.keys.map((k) => (
                  <li key={k.id} className="flex min-w-0 flex-wrap items-center justify-between gap-2 py-1.5 text-caption">
                    <span className="min-w-0 break-all text-foreground">{k.name} <span className="font-mono">{k.prefix}…</span> · {k.status}</span>
                    <span className="min-w-0 break-words text-muted-foreground">{k.workflow_ids.map(nameOf).join(", ")} · expires {when(k.expires_at)} · last used {when(k.last_used_at)}</span>
                    {canManage && k.status === "active" ? <Button variant="secondary" size="sm" disabled={busy} aria-label={`Revoke ${k.name}`} onClick={() => setConfirm({ revoke: k.id })}>Revoke</Button> : null}
                  </li>
                ))}
              </ul>
            )}
            {secret ? (
              <div data-testid="external-key-secret" className="space-y-1">
                <CopyField label="New key (shown once)" value={secret} copiedMessage="Key copied" hint="Put it in the agent now; if you lose it, make another and revoke this one." />
                <Button variant="secondary" size="sm" onClick={() => setSecret(null)} data-testid="external-key-done">I copied it</Button>
              </div>
            ) : null}
            {canManage ? (
              <form onSubmit={createKey} className="min-w-0 space-y-2 rounded-md border border-border p-3" aria-label="New key">
                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-1">
                    <label htmlFor={`${id}-name`} className={labelClass}>Key name</label>
                    <input id={`${id}-name`} data-testid="external-key-name" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} className={fieldClass} />
                  </div>
                  <div className="space-y-1">
                    <label htmlFor={`${id}-days`} className={labelClass}>Expires after (days)</label>
                    <input id={`${id}-days`} type="number" min={1} max={365} value={days} onChange={(e) => setDays(Math.min(365, Math.max(1, Number(e.target.value) || KEY_DAYS)))} className={fieldClass} />
                  </div>
                </div>
                <fieldset className="min-w-0 space-y-1">
                  <legend className={labelClass}>Workflows this key may run</legend>
                  <div className="max-h-48 overflow-auto" tabIndex={0} role="group" aria-label="Workflow list">
                    {workflows.map((w) => (
                      <label key={w.id} className="flex items-center gap-2 text-label text-foreground">
                        <input type="checkbox" checked={chosen.includes(w.id)} onChange={(e) => setChosen((c) => (e.target.checked ? [...c, w.id] : c.filter((x) => x !== w.id)))} />
                        {w.name}
                      </label>
                    ))}
                  </div>
                  <p className={hintClass}>A key only runs exports of these workflows. It reaches no agent and nothing else on the platform.</p>
                </fieldset>
                <Button variant="primary" size="sm" type="submit" disabled={busy || !name.trim() || chosen.length === 0} data-testid="external-key-submit">Create key</Button>
              </form>
            ) : null}
          </section>

          {canManage ? (
            <div className="space-y-1 border-t border-border pt-3">
              <Button variant="secondary" size="sm" disabled={busy} onClick={() => setConfirm("cancel")} data-testid="external-cancel-runs">Cancel this client&apos;s runs</Button>
              {result ? <p role="status" className={hintClass} data-testid="external-cancel-result">{result}</p> : null}
            </div>
          ) : null}
        </SheetBody>
      </SheetContent>
      {confirm === "cancel" ? (
        <ConfirmDialog title={`Cancel the runs of ${client.name}?`} description="Every run of this client that has not ended is cancelled. A run cancelled in the middle of an external action may need a person." confirmLabel="Cancel runs" cancelLabel="Keep them" destructive busy={busy} onCancel={() => setConfirm(null)}
          onConfirm={() => void run(async () => {
            const done = await externalAccess.cancelRuns(client.id);
            setResult(`${done.cancelled.length} runs cancelled${done.pending.length ? `, ${done.pending.length} still finishing` : ""}.`);
          })} />
      ) : null}
      {confirm !== null && confirm !== "cancel" ? (
        <ConfirmDialog title="Revoke this key?" description="It stops working at the next request. Runs go on, and the client's other keys still reach them." confirmLabel="Revoke" destructive busy={busy} onCancel={() => setConfirm(null)}
          onConfirm={() => void run(() => externalAccess.revokeKey(client.id, confirm.revoke))} />
      ) : null}
    </Sheet>
  );
}

function ExportDialog({ workflows, existing, onSaved, onClose }: { workflows: WorkflowOption[]; existing: WorkflowExport | null; onSaved: (e: WorkflowExport) => void; onClose: () => void }) {
  const id = useId();
  const [name, setName] = useState(existing?.name ?? "");
  const [title, setTitle] = useState(existing?.title ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [workflowId, setWorkflowId] = useState(existing?.workflow_id ?? workflows[0]?.id ?? "");
  const [version, setVersion] = useState(String(existing?.version ?? "latest"));
  const [protocols, setProtocols] = useState<Protocol[]>(existing?.protocols ?? ["mcp"]);
  const [result, setResult] = useState<ResultMode>(existing?.result ?? "output");
  const [runtime, setRuntime] = useState(existing?.max_runtime_seconds ?? 3600);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    const body: ExportInput = {
      name, title, description, workflow_id: workflowId, version: version.trim() === "latest" ? "latest" : Number(version),
      protocols, result, max_runtime_seconds: runtime,
    };
    try {
      if (existing) {
        const { workflow_id: _fixed, ...changes } = body;
        void _fixed;
        onSaved(await externalAccess.updateExport(existing.id, changes, existing.revision));
      } else {
        onSaved(await externalAccess.createExport(body));
      }
    } catch (reason: unknown) {
      setError(message(reason, "The export could not be saved"));
      setSaving(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
      <DialogContent className="md:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{existing ? "Edit export" : "Export a workflow"}</DialogTitle>
          <DialogDescription>
            The public contract of a published workflow. Write the title and description for the outside. {existing ? "Saving a change disables the export." : "It is created disabled; an admin enables it after reviewing it."}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(event) => void submit(event)} className="min-w-0 space-y-4" aria-label="Export a workflow">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={`${id}-name`} className={labelClass}>Public name</label>
              <input id={`${id}-name`} data-testid="external-export-name" value={name} maxLength={48} required onChange={(e) => setName(e.target.value)} className={fieldClass} placeholder="run_report" aria-describedby={`${id}-name-hint`} />
              <p id={`${id}-name-hint`} className={hintClass}>Lower-case letters, digits and _. The tool and skill id outside.</p>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-title`} className={labelClass}>Title</label>
              <input id={`${id}-title`} data-testid="external-export-title" value={title} maxLength={200} required onChange={(e) => setTitle(e.target.value)} className={fieldClass} />
            </div>
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-description`} className={labelClass}>Description</label>
            <textarea id={`${id}-description`} value={description} maxLength={2000} rows={3} onChange={(e) => setDescription(e.target.value)} className={fieldClass} />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={`${id}-workflow`} className={labelClass}>Workflow</label>
              <select id={`${id}-workflow`} data-testid="external-export-workflow" value={workflowId} disabled={existing !== null} onChange={(e) => setWorkflowId(e.target.value)} className={fieldClass}>
                {workflows.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
              </select>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-version`} className={labelClass}>Version</label>
              <input id={`${id}-version`} value={version} onChange={(e) => setVersion(e.target.value)} className={fieldClass} aria-describedby={`${id}-version-hint`} />
              <p id={`${id}-version-hint`} className={hintClass}>A version number, or &quot;latest&quot;.</p>
            </div>
          </div>
          <fieldset className="min-w-0 space-y-1">
            <legend className={labelClass}>Protocols</legend>
            {(["mcp", "a2a"] as const).map((p) => (
              <label key={p} className="mr-4 inline-flex items-center gap-2 text-label text-foreground">
                <input type="checkbox" checked={protocols.includes(p)} onChange={(e) => setProtocols((c) => (e.target.checked ? [...c, p] : c.filter((x) => x !== p)))} />
                {p === "mcp" ? "MCP" : "A2A"}
              </label>
            ))}
          </fieldset>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={`${id}-result`} className={labelClass}>Result</label>
              <select id={`${id}-result`} value={result} onChange={(e) => setResult(e.target.value as ResultMode)} className={fieldClass}>
                <option value="output">The workflow&apos;s return value</option>
                <option value="status_only">Status only</option>
              </select>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-runtime`} className={labelClass}>Longest run (seconds)</label>
              <input id={`${id}-runtime`} type="number" min={1} value={runtime} onChange={(e) => setRuntime(Number(e.target.value))} className={fieldClass} />
            </div>
          </div>
          {error ? <p role="alert" className={alertClass}>{error}</p> : null}
          <DialogFooter>
            <Button variant="secondary" type="button" onClick={onClose} disabled={saving}>Cancel</Button>
            <Button variant="primary" type="submit" disabled={saving || !name || !title || !workflowId || protocols.length === 0} data-testid="external-export-submit">{existing ? "Save" : "Create export"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function ExportSheet({ item, settings, onChanged, onEdit, onReload, onClose }: { item: WorkflowExport; settings: ExternalSettings | null; onChanged: (e: WorkflowExport) => void; onEdit: () => void; onReload: () => Promise<void>; onClose: () => void }) {
  const canManage = useCan("tenant", null, "manage_external_access");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"enable" | "disable" | null>(null);
  const ready = Boolean(settings?.base_url && settings.grants_enforced && item.approval_hash);

  const run = async (action: () => Promise<WorkflowExport>) => {
    setBusy(true);
    setError(null);
    try {
      onChanged(await action());
    } catch (reason: unknown) {
      setError(message(reason, "The change was refused"));
      await onReload(); // what the card shows may be what changed
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent size="wide" data-testid="external-export-detail">
        <SheetHeader title={item.name} description={item.title} />
        <SheetBody className="space-y-5">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge enabled={item.enabled} testId="external-export-status" />
            {item.enabled ? <span className={hintClass}>enabled by {item.enabled_by} on {when(item.enabled_at)}</span> : null}
          </div>
          {!item.enabled && item.disabled_reason ? <p className={warningClass} data-testid="external-disabled-reason">{DISABLED_REASONS[item.disabled_reason] ?? item.disabled_reason}</p> : null}
          {error ? <p role="alert" className={alertClass}>{error}</p> : null}

          <section aria-label="Enable card" className="space-y-2 rounded-md border border-border p-3" data-testid="external-enable-card">
            <h3 className="text-label font-medium text-foreground">{item.enabled ? "What is approved" : "Review before enabling"}</h3>
            <p className={hintClass}>
              An allowlisted key can start <strong>{item.workflow_name ?? item.workflow_id}</strong>, version {item.resolved_version ?? "none"}
              {item.version === "latest" ? " (follows the latest)" : " (pinned)"}, over {item.protocols.map((p) => p.toUpperCase()).join(" and ")}. The run uses the workflow&apos;s grants.
            </p>
            <p className={hintClass}>
              {item.result === "output" ? "The workflow's return value leaves the platform: check it carries no secret or personal data." : "Status only: the workflow's return value does not leave the platform."}
              {" "}A run lasts at most {item.max_runtime_seconds} s{item.require_idempotency_key ? " and needs an idempotency key" : ""}. Approvals are never decided from outside.
            </p>
            {item.warnings.length > 0 ? (
              <ul className="space-y-1" data-testid="external-warnings">
                {item.warnings.map((w) => <li key={w.code} className={warningClass}>{w.message}</li>)}
              </ul>
            ) : null}
            <div className="min-w-0">
              <h4 className="text-caption font-medium text-foreground">Input schema</h4>
              <pre tabIndex={0} aria-label="Input schema" className="max-h-48 overflow-auto rounded-md border border-border bg-muted/40 p-2 font-mono text-caption text-foreground" data-testid="external-input-schema">{JSON.stringify(item.input_schema, null, 2)}</pre>
            </div>
            <div data-testid="external-reach">
              <h4 className="text-caption font-medium text-foreground">Keys that can reach it</h4>
              {item.keys.length === 0 ? <p className={hintClass}>None yet.</p> : (
                <ul className="text-caption text-foreground">
                  {item.keys.map((k) => <li key={k.id}>{k.client_name}: {k.name} <span className="font-mono">{k.prefix}…</span></li>)}
                </ul>
              )}
            </div>
            <p className="min-w-0 break-all font-mono text-caption text-muted-foreground" data-testid="external-hash">approval {item.approval_hash?.slice(0, 16) ?? "unavailable"}…</p>
            {!canManage ? <p className={hintClass} data-testid="external-needs-admin">An admin enables an export.</p> : null}
            {canManage ? (
              <div className="flex flex-wrap gap-2">
                {item.enabled
                  ? <Button variant="secondary" size="sm" disabled={busy} onClick={() => setConfirm("disable")} data-testid="external-disable">Disable</Button>
                  : <Button variant="primary" size="sm" disabled={busy || !ready} onClick={() => setConfirm("enable")} data-testid="external-enable">Enable</Button>}
                <Button variant="secondary" size="sm" disabled={busy} onClick={onEdit} data-testid="external-edit">Edit</Button>
              </div>
            ) : null}
          </section>
        </SheetBody>
      </SheetContent>
      {confirm === "enable" && item.approval_hash ? (
        <ConfirmDialog title={`Enable ${item.name}?`} description="From now on an external client with an allowlisted key can start this workflow." confirmLabel="Enable" busy={busy} onCancel={() => setConfirm(null)}
          onConfirm={() => void run(() => externalAccess.enable(item.id, item.approval_hash as string))} />
      ) : null}
      {confirm === "disable" ? (
        <ConfirmDialog title={`Disable ${item.name}?`} description="No new run starts from the next request. Runs already started go on." confirmLabel="Disable" busy={busy} onCancel={() => setConfirm(null)}
          onConfirm={() => void run(() => externalAccess.disable(item.id))} />
      ) : null}
    </Sheet>
  );
}

/** External access: the clients external agents act as, their keys, and the workflows exported to them. */
export function ExternalAccessPage() {
  const [settings, setSettings] = useState<ExternalSettings | null>(null);
  const [clients, setClients] = useState<ExternalClient[] | null>(null);
  const [exports, setExports] = useState<WorkflowExport[] | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowOption[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState<"client" | "export" | null>(null);
  const [editing, setEditing] = useState(false);
  const [openClient, setOpenClient] = useState<string | null>(null);
  const [openExport, setOpenExport] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [status, c, e, wf] = await Promise.all([externalAccess.settings(), externalAccess.clients(), externalAccess.exports(), listWorkflows().catch(() => [])]);
      setSettings(status);
      setClients(c);
      setExports(e);
      setWorkflows(wf.map((w) => ({ id: w.id, name: w.name })));
      setError(null);
    } catch (reason: unknown) {
      setError(message(reason, "Could not load external access"));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const replace = (item: WorkflowExport) => setExports((current) => (current ?? []).some((i) => i.id === item.id) ? (current ?? []).map((i) => (i.id === item.id ? item : i)) : [item, ...(current ?? [])]);
  const client = clients?.find((c) => c.id === openClient) ?? null;
  const exported = exports?.find((e) => e.id === openExport) ?? null;

  return (
    <>
      <PageHeader crumbs={[navCrumb("external-access", { current: true })]} />
      <div className="mx-auto w-full max-w-5xl space-y-6 p-4" data-testid="external-access-page">
        <p className={hintClass}>External agents run the workflows you export, and nothing else. An export starts disabled and a person enables it.</p>
        {settings ? <PlatformStatus settings={settings} /> : null}
        {error ? <p role="alert" className={alertClass} data-testid="external-error">{error}</p> : null}
        {clients === null && !error ? <p className={hintClass}>Loading…</p> : null}

        {exports !== null ? (
          <section aria-labelledby="external-exports-title" className="space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="external-exports-title" className="text-label font-semibold text-foreground">Workflow exports</h2>
              <Can kind="tenant" action="manage_external_access">
                <Button variant="primary" size="sm" onClick={() => setCreating("export")} data-testid="external-export-create"><Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />Export a workflow</Button>
              </Can>
            </div>
            {exports.length === 0 ? <EmptyState title="No workflow is exported" description="Export a published workflow to let an external agent run it." testId="external-exports-empty" /> : (
              <ul className="divide-y divide-border rounded-md border border-border">
                {exports.map((e) => (
                  <li key={e.id} className="flex min-w-0 flex-wrap items-center justify-between gap-2 px-3 py-2.5" data-testid="external-export-row">
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <button type="button" className={rowButtonClass} onClick={() => setOpenExport(e.id)}>{e.name}</button>
                      <span className="text-caption text-muted-foreground">{e.workflow_name ?? e.workflow_id} · {e.version === "latest" ? "latest" : `v${e.version}`} · {e.protocols.map((p) => p.toUpperCase()).join(", ")}</span>
                    </div>
                    <StatusBadge enabled={e.enabled} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        ) : null}

        {clients !== null ? <A2AEndpointSection settings={settings} /> : null}

        {clients !== null ? (
          <section aria-labelledby="external-clients-title" className="space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="external-clients-title" className="text-label font-semibold text-foreground">External clients</h2>
              <Can kind="tenant" action="manage_external_access">
                <Button variant="secondary" size="sm" onClick={() => setCreating("client")} data-testid="external-client-create"><Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />New client</Button>
              </Can>
            </div>
            {clients.length === 0 ? <EmptyState title="No external client" description="Create a client for each external agent, then give it a key." testId="external-clients-empty" /> : (
              <ul className="divide-y divide-border rounded-md border border-border">
                {clients.map((c) => (
                  <li key={c.id} className="flex min-w-0 flex-wrap items-center justify-between gap-2 px-3 py-2.5" data-testid="external-client-row">
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <button type="button" className={rowButtonClass} onClick={() => setOpenClient(c.id)}>{c.name}</button>
                      <span className="text-caption text-muted-foreground">{c.keys.filter((k) => k.status === "active").length} active keys</span>
                    </div>
                    <StatusBadge enabled={c.enabled} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        ) : null}
      </div>
      {creating === "client" ? <CreateClientDialog onCreated={(c) => setClients((current) => [...(current ?? []), c])} onClose={() => setCreating(null)} /> : null}
      {creating === "export" ? <ExportDialog workflows={workflows} existing={null} onSaved={(e) => { replace(e); setCreating(null); setOpenExport(e.id); }} onClose={() => setCreating(null)} /> : null}
      {client ? <ClientSheet client={client} workflows={workflows} onReload={load} onClose={() => setOpenClient(null)} /> : null}
      {exported && !editing ? <ExportSheet item={exported} settings={settings} onChanged={replace} onEdit={() => setEditing(true)} onReload={load} onClose={() => setOpenExport(null)} /> : null}
      {exported && editing ? <ExportDialog workflows={workflows} existing={exported} onSaved={(e) => { replace(e); setEditing(false); }} onClose={() => setEditing(false)} /> : null}
    </>
  );
}
