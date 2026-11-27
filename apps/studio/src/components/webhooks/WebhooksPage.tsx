"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { PageHeader } from "@/components/frame/PageHeader";
import { CopyField } from "@/components/apiKeys/CopyField";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { EmptyState } from "@/components/shared/EmptyState";
import { Badge, type BadgeProps } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Can, useCan } from "@/lib/permissions";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { listWorkflows } from "@/lib/workflows";
import {
  OUTCOME_LABELS,
  PRESET_HEADERS,
  PRESET_LABELS,
  STATUS_LABELS,
  parseJsonObject,
  webhookWhen,
  webhooks,
  type WebhookCreate,
  type WebhookCreated,
  type WebhookDelivery,
  type WebhookEvent,
  type WebhookPreset,
  type WebhookStatus,
  type WebhookTrigger,
} from "@/lib/webhooks";

const fieldClass =
  "w-full min-w-0 rounded-md border border-input bg-surface px-3 py-1.5 text-label text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const labelClass = "block text-label font-medium text-foreground";
const hintClass = "text-caption text-muted-foreground";
const alertClass = "rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text";

const STATUS_VARIANT: Record<WebhookStatus, BadgeProps["variant"]> = { enabled: "success", pending_enable: "warning", disabled: "neutral", archived: "neutral" };

const DEFAULT_SCHEMA = '{\n  "type": "object",\n  "required": ["repository"],\n  "properties": { "repository": { "type": "object" } }\n}';
const DEFAULT_MAP = '{\n  "repo": { "$pointer": "/repository/full_name" }\n}';
const DEFAULT_CUSTOM = '{\n  "header": "x-signature-256",\n  "prefix": "sha256=",\n  "timestamp": { "header": "x-timestamp" },\n  "signed_payload": "{timestamp}.{body}"\n}';

function message(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

interface WorkflowOption {
  id: string;
  name: string;
}

function CreateDialog({ workflows, onCreated, onClose }: { workflows: WorkflowOption[]; onCreated: (trigger: WebhookTrigger) => void; onClose: () => void }) {
  const id = useId();
  const [name, setName] = useState("");
  const [workflowId, setWorkflowId] = useState(workflows[0]?.id ?? "");
  const [version, setVersion] = useState("latest");
  const [preset, setPreset] = useState<WebhookPreset>("github");
  const [custom, setCustom] = useState(DEFAULT_CUSTOM);
  const canGenerate = useCan("secret", null, "create");
  const [chosenMode, setSecretMode] = useState<"generate" | "existing">("generate");
  const secretMode = canGenerate ? chosenMode : "existing";
  const [secretName, setSecretName] = useState("");
  const [schema, setSchema] = useState(DEFAULT_SCHEMA);
  const [map, setMap] = useState(DEFAULT_MAP);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The generated secret: only here, only while this dialog is open.
  const [created, setCreated] = useState<WebhookCreated | null>(null);
  const secretRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (created) secretRef.current?.querySelector<HTMLElement>("[data-copy-button]")?.focus();
  }, [created]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    const payloadSchema = parseJsonObject("The payload schema", schema);
    const inputMap = parseJsonObject("The input mapping", map);
    const presetConfig = preset === "custom" ? parseJsonObject("The preset config", custom) : null;
    for (const parsed of [payloadSchema, inputMap, presetConfig]) {
      if (parsed && !parsed.ok) return setError(parsed.message);
    }
    if (secretMode === "existing" && !/^[a-z][a-z0-9_.-]{0,63}$/.test(secretName)) return setError("Enter the name of an existing secret (lower-case letters, digits, . _ -), not its value.");
    const body: WebhookCreate = {
      name,
      workflow_id: workflowId,
      version: version === "latest" ? "latest" : Number(version),
      preset,
      actions: { start: { payload_schema: (payloadSchema as { value: Record<string, unknown> }).value, input_map: (inputMap as { value: Record<string, unknown> }).value } },
      ...(presetConfig ? { preset_config: (presetConfig as { value: Record<string, unknown> }).value } : {}),
      ...(secretMode === "generate" ? { generate: true } : { secrets: [`secret:${secretName}`] }),
    };
    setSaving(true);
    setError(null);
    try {
      const result = await webhooks.create(body);
      setCreated(result);
      const { generated_secret: _drop, ...listed } = result;
      void _drop;
      onCreated(listed);
      if (!result.generated_secret) onClose();
    } catch (reason: unknown) {
      setError(message(reason, "The trigger could not be created"));
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
      <DialogContent className="md:max-w-2xl" data-testid="webhook-create-dialog">
        {created?.generated_secret ? (
          <>
            <DialogHeader>
              <DialogTitle>Copy the signing secret</DialogTitle>
              <DialogDescription>
                This is the only time the secret is shown. Put it in the sender (the provider&apos;s webhook settings) now; if you lose it, rotate to a new one.
              </DialogDescription>
            </DialogHeader>
            <div ref={secretRef} data-testid="webhook-secret">
              <CopyField label={`${created.name} signing secret`} value={created.generated_secret} copiedMessage="Secret copied" />
            </div>
            {created.url ? <CopyField label="Payload URL" value={created.url} copiedMessage="URL copied" testId="webhook-url" /> : null}
            <p className={hintClass}>The trigger is disabled. Open it and enable it when the sender is ready.</p>
            <DialogFooter>
              <Button variant="primary" onClick={close} data-testid="webhook-done">Done</Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Create webhook trigger</DialogTitle>
              <DialogDescription>
                An outside system starts a published workflow with a signed request. The trigger is created disabled; you enable it after reviewing it.
              </DialogDescription>
            </DialogHeader>
            <form onSubmit={(event) => void submit(event)} className="min-w-0 space-y-4" aria-label="Create webhook trigger" data-testid="webhook-create-form">
              <div className="space-y-1">
                <label htmlFor={`${id}-name`} className={labelClass}>Name</label>
                <input id={`${id}-name`} data-testid="webhook-name" value={name} maxLength={64} required onChange={(e) => setName(e.target.value)} className={fieldClass} placeholder="github-push" />
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <label htmlFor={`${id}-workflow`} className={labelClass}>Workflow</label>
                  <select id={`${id}-workflow`} data-testid="webhook-workflow" value={workflowId} required onChange={(e) => setWorkflowId(e.target.value)} className={fieldClass}>
                    {workflows.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                  </select>
                </div>
                <div className="space-y-1">
                  <label htmlFor={`${id}-version`} className={labelClass}>Version</label>
                  <input id={`${id}-version`} data-testid="webhook-version" value={version} onChange={(e) => setVersion(e.target.value)} className={fieldClass} aria-describedby={`${id}-version-hint`} />
                  <p id={`${id}-version-hint`} className={hintClass}>A version number, or &quot;latest&quot; (any later publish then changes what the outside system can start).</p>
                </div>
              </div>
              <div className="space-y-1">
                <label htmlFor={`${id}-preset`} className={labelClass}>Signature</label>
                <select id={`${id}-preset`} data-testid="webhook-preset" value={preset} onChange={(e) => setPreset(e.target.value as WebhookPreset)} className={fieldClass}>
                  {(Object.keys(PRESET_LABELS) as WebhookPreset[]).map((p) => <option key={p} value={p}>{PRESET_LABELS[p]}</option>)}
                </select>
                <p className={hintClass}>The sender sets {PRESET_HEADERS[preset]}.</p>
              </div>
              {preset === "custom" ? (
                <div className="space-y-1">
                  <label htmlFor={`${id}-custom`} className={labelClass}>Preset config (JSON)</label>
                  <textarea id={`${id}-custom`} data-testid="webhook-custom" value={custom} onChange={(e) => setCustom(e.target.value)} rows={6} className={`${fieldClass} font-mono text-caption`} />
                </div>
              ) : null}
              <fieldset className="min-w-0 space-y-1">
                <legend className={labelClass}>Signing secret</legend>
                {canGenerate ? (
                  <label className="flex items-center gap-2 text-label text-foreground">
                    <input type="radio" name={`${id}-secret`} checked={secretMode === "generate"} onChange={() => setSecretMode("generate")} data-testid="webhook-secret-generate" />
                    Generate one (shown once)
                  </label>
                ) : null}
                <label className="flex items-center gap-2 text-label text-foreground">
                  <input type="radio" name={`${id}-secret`} checked={secretMode === "existing"} onChange={() => setSecretMode("existing")} data-testid="webhook-secret-existing" />
                  Use an existing secret
                </label>
                {secretMode === "existing" ? (
                  <input aria-label="Secret name" data-testid="webhook-secret-name" value={secretName} onChange={(e) => setSecretName(e.target.value)} className={fieldClass} placeholder="provider-signing-secret" />
                ) : null}
                <p className={hintClass}>Enter the secret&apos;s name, never its value. Provider-issued secrets are added under Settings → Secrets first.</p>
              </fieldset>
              <div className="space-y-1">
                <label htmlFor={`${id}-schema`} className={labelClass}>Payload schema (JSON Schema)</label>
                <textarea id={`${id}-schema`} data-testid="webhook-schema" value={schema} onChange={(e) => setSchema(e.target.value)} rows={5} className={`${fieldClass} font-mono text-caption`} />
              </div>
              <div className="space-y-1">
                <label htmlFor={`${id}-map`} className={labelClass}>Input mapping</label>
                <textarea id={`${id}-map`} data-testid="webhook-map" value={map} onChange={(e) => setMap(e.target.value)} rows={4} className={`${fieldClass} font-mono text-caption`} aria-describedby={`${id}-map-hint`} />
                <p id={`${id}-map-hint`} className={hintClass}>Leaves are values, {"{"}&quot;$pointer&quot;: &quot;/path&quot;{"}"} into the payload, or {"{"}&quot;$payload&quot;: true{"}"}.</p>
              </div>
              {error ? <p role="alert" className={alertClass} data-testid="webhook-create-error">{error}</p> : null}
              <DialogFooter>
                <Button variant="secondary" type="button" onClick={close} disabled={saving}>Cancel</Button>
                <Button variant="primary" type="submit" disabled={saving || !name || !workflowId} data-testid="webhook-create-submit">{saving ? "Creating…" : "Create trigger"}</Button>
              </DialogFooter>
            </form>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Detail({ trigger, workflowName, onChanged, onClose }: { trigger: WebhookTrigger; workflowName: string; onChanged: (t: WebhookTrigger) => void; onClose: () => void }) {
  const [full, setFull] = useState<WebhookTrigger>(trigger);
  const [events, setEvents] = useState<WebhookEvent[]>([]);
  const [deliveries, setDeliveries] = useState<WebhookDelivery[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"enable" | "disable" | "archive" | null>(null);
  const [addRef, setAddRef] = useState("");
  const [rotated, setRotated] = useState<string | null>(null);

  // What the server would allow (it decides again): changing a trigger is editing its workflow; approving one is
  // granting its secrets; generating a secret is creating one.
  const canEdit = useCan("workflow", full.workflow_id, "edit");
  const canApprove = useCan("secret", full.secrets[0]?.ref.replace(/^secret:/, "") ?? null, "grant_use");
  const canGenerate = useCan("secret", null, "create");
  const canAdd = canEdit && (!full.approved || canApprove);

  const refresh = useCallback(async () => {
    try {
      const [fresh, ev, del] = await Promise.all([webhooks.get(trigger.id), webhooks.events(trigger.id), webhooks.deliveries(trigger.id)]);
      setFull(fresh);
      setEvents(ev);
      setDeliveries(del);
    } catch (reason: unknown) {
      setError(message(reason, "Could not load the trigger"));
    }
  }, [trigger.id]);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const run = async (action: () => Promise<WebhookTrigger | WebhookCreated>) => {
    setBusy(true);
    setError(null);
    try {
      const result = await action();
      const { generated_secret: secret, ...listed } = result as WebhookCreated;
      if (secret) setRotated(secret);
      setFull(listed);
      onChanged(listed);
      await refresh();
      return true;
    } catch (reason: unknown) {
      setError(message(reason, "The change was refused"));
      return false;
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const open = full.status === "disabled" || full.status === "pending_enable";
  return (
    <Sheet open onOpenChange={(next) => { if (!next) onClose(); }}>
      <SheetContent size="wide" data-testid="webhook-detail">
        <SheetHeader title={full.name} description={`${PRESET_LABELS[full.preset]} → ${workflowName} (${full.version === "latest" ? "latest" : `v${full.version}`})`} />
        <SheetBody className="space-y-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={STATUS_VARIANT[full.status]} data-testid="webhook-status">{STATUS_LABELS[full.status]}</Badge>
            {full.status === "enabled" ? <span className={hintClass}>enabled by {full.enabled_by} on {webhookWhen(full.enabled_at)}</span> : null}
          </div>
          {error ? <p role="alert" className={alertClass} data-testid="webhook-error">{error}</p> : null}
          {full.url ? <CopyField label="Payload URL" value={full.url} copiedMessage="URL copied" testId="webhook-url" hint={`The sender signs with ${PRESET_HEADERS[full.preset]}.`} /> : <p className={hintClass}>Set EAP_WEBHOOK_BASE_URL on the API to show and enable the public URL.</p>}
          {rotated ? (
            <div data-testid="webhook-rotated-secret">
              <CopyField label="New signing secret (shown once)" value={rotated} copiedMessage="Secret copied" />
              <Button variant="secondary" size="sm" onClick={() => setRotated(null)}>I copied it</Button>
            </div>
          ) : null}

          {open ? (
            <section aria-label="Enable card" className="space-y-2 rounded-md border border-border p-3" data-testid="webhook-enable-card">
              <h3 className="text-label font-medium text-foreground">{full.status === "pending_enable" ? `Enable requested by ${full.enable_requested_by}` : "Review before enabling"}</h3>
              <p className={hintClass}>Once enabled, anyone who holds the signing secret can start a run of <strong>{workflowName}</strong> from this URL, up to {full.limits.max_concurrent_runs} at a time and {full.limits.max_runs_per_day} a day. The workflow keeps its own powers; the payload only becomes its input through the mapping below.</p>
              {(full.warnings ?? []).length > 0 ? (
                <ul className="space-y-1" data-testid="webhook-warnings">
                  {(full.warnings ?? []).map((w) => <li key={w.code} className="rounded-md border border-warning/30 bg-warning/10 p-2 text-caption text-foreground">{w.message}</li>)}
                </ul>
              ) : null}
              <p className="min-w-0 break-all font-mono text-caption text-muted-foreground" data-testid="webhook-hash">config {full.config_hash.slice(0, 16)}…</p>
              {!canEdit || !canApprove ? <p className={hintClass} data-testid="webhook-needs-admin">An admin enables a trigger: enabling approves the use of its signing secret.</p> : null}
              <div className="flex gap-2">
                {canEdit && canApprove ? <Button variant="primary" size="sm" onClick={() => setConfirm("enable")} disabled={busy || !full.url} data-testid="webhook-enable">Enable</Button> : null}
                {canEdit && full.status === "pending_enable" ? (
                  <Button variant="secondary" size="sm" onClick={() => void run(() => webhooks.decide(full.id, "reject", full.config_hash))} disabled={busy} data-testid="webhook-reject">Reject request</Button>
                ) : null}
              </div>
            </section>
          ) : null}

          <section aria-label="Configuration" className="space-y-1">
            <h3 className="text-label font-medium text-foreground">What it does</h3>
            <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-caption">
              <dt className="text-muted-foreground">Limits</dt>
              <dd className="text-foreground">{full.limits.rate_per_minute}/min, {full.limits.max_concurrent_runs} concurrent, {full.limits.max_runs_per_day}/day, body ≤ {Math.round(full.limits.max_body_bytes / 1024)} KiB</dd>
              <dt className="text-muted-foreground">Tolerance</dt>
              <dd className="text-foreground">{full.tolerance_seconds} s</dd>
              <dt className="text-muted-foreground">Input mapping</dt>
              <dd className="min-w-0"><pre className="max-h-40 overflow-auto font-mono text-caption text-foreground">{JSON.stringify(full.actions.start.input_map, null, 2)}</pre></dd>
              <dt className="text-muted-foreground">Last delivery</dt>
              <dd className="text-foreground">{webhookWhen(full.last_delivery_at)}{full.last_outcome ? ` (${OUTCOME_LABELS[full.last_outcome] ?? full.last_outcome})` : ""}</dd>
            </dl>
            {full.refusals_since_start && Object.keys(full.refusals_since_start).length > 0 ? (
              <p className={hintClass} data-testid="webhook-refusals">Refused since the API started: {Object.entries(full.refusals_since_start).map(([k, n]) => `${k} ${n}`).join(", ")}</p>
            ) : null}
          </section>

          <section aria-label="Secrets" className="space-y-1">
            <h3 className="text-label font-medium text-foreground">Signing secrets</h3>
            <ul className="space-y-1" data-testid="webhook-secrets">
              {full.secrets.map((s) => (
                <li key={s.ref} className="flex min-w-0 flex-wrap items-center justify-between gap-2 text-caption">
                  <span className="min-w-0 break-all font-mono text-foreground">{s.ref}</span>
                  <span className="text-muted-foreground">last matched {webhookWhen(s.last_matched_at)}</span>
                  {canEdit && full.secrets.length > 1 ? <Button variant="secondary" size="sm" disabled={busy} onClick={() => void run(() => webhooks.rotate(full.id, { retire: s.ref }))} aria-label={`Retire ${s.ref}`}>Retire</Button> : null}
                </li>
              ))}
            </ul>
            {canAdd && full.status !== "archived" && full.secrets.length < 2 ? (
              <div className="flex flex-wrap items-center gap-2">
                <input aria-label="Secret name to add" value={addRef} onChange={(e) => setAddRef(e.target.value)} className={`${fieldClass} max-w-64`} placeholder="new-secret-name" data-testid="webhook-add-ref" />
                <Button variant="secondary" size="sm" disabled={busy || !addRef} onClick={() => void run(() => webhooks.rotate(full.id, { add: `secret:${addRef}` })).then((ok) => ok && setAddRef(""))}>Add as second secret</Button>
                {canGenerate ? <Button variant="secondary" size="sm" disabled={busy} onClick={() => void run(() => webhooks.rotate(full.id, { generate: true }))} data-testid="webhook-rotate-generate">Generate a new one</Button> : null}
              </div>
            ) : null}
            <p className={hintClass}>Rotation without downtime: add the new secret, update the sender, then retire the old one.</p>
          </section>

          <section aria-label="Deliveries" className="space-y-1">
            <h3 className="text-label font-medium text-foreground">Deliveries</h3>
            {deliveries.length === 0 ? <p className={hintClass}>No authenticated delivery yet.</p> : (
              <ul className="divide-y divide-border" data-testid="webhook-deliveries">
                {deliveries.map((d) => (
                  <li key={d.id} className="flex min-w-0 flex-wrap items-center justify-between gap-2 py-1.5 text-caption" data-testid="webhook-delivery">
                    <span className="text-foreground">{OUTCOME_LABELS[d.outcome] ?? d.outcome}{d.reason ? ` · ${d.reason}` : ""}</span>
                    <span className="text-muted-foreground">{webhookWhen(d.received_at)} · {d.body_bytes ?? 0} B{d.event_label ? ` · ${d.event_label} (unverified)` : ""}</span>
                    {d.run_id ? <a className="text-primary underline" href={routes.run(d.run_id)}>run</a> : null}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section aria-label="History" className="space-y-1">
            <h3 className="text-label font-medium text-foreground">History</h3>
            <ul className="divide-y divide-border" data-testid="webhook-events">
              {events.map((e) => (
                <li key={e.id} className="flex min-w-0 flex-wrap items-center justify-between gap-2 py-1 text-caption">
                  <span className="text-foreground">{e.kind.replace(/_/g, " ")}</span>
                  <span className="text-muted-foreground">{e.actor} · {webhookWhen(e.at)}</span>
                </li>
              ))}
            </ul>
          </section>

          {canEdit && full.status !== "archived" ? (
            <div className="flex flex-wrap gap-2 border-t border-border pt-3">
              {full.status === "enabled" ? <Button variant="secondary" size="sm" disabled={busy} onClick={() => setConfirm("disable")} data-testid="webhook-disable">Disable</Button> : null}
              <Button variant="secondary" size="sm" disabled={busy} onClick={() => setConfirm("archive")} data-testid="webhook-archive">Archive</Button>
            </div>
          ) : null}
        </SheetBody>
      </SheetContent>
      {confirm === "enable" ? (
        <ConfirmDialog title={`Enable ${full.name}?`} description="From now on this URL accepts requests signed with the secret and starts runs of the workflow." confirmLabel="Enable" busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void run(() => webhooks.enable(full.id, full.config_hash))} />
      ) : null}
      {confirm === "disable" ? (
        <ConfirmDialog title={`Disable ${full.name}?`} description="The URL answers 404 from the next request. Runs already started continue." confirmLabel="Disable" busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void run(() => webhooks.disable(full.id))} />
      ) : null}
      {confirm === "archive" ? (
        <ConfirmDialog title={`Archive ${full.name}?`} description="The trigger is retired for good; its history stays." confirmLabel="Archive" destructive busy={busy} onCancel={() => setConfirm(null)} onConfirm={() => void run(() => webhooks.archive(full.id))} />
      ) : null}
    </Sheet>
  );
}

/** Webhooks: the tenant's inbound triggers, create, the enable card, rotation, the delivery log. */
export function WebhooksPage() {
  const [items, setItems] = useState<WebhookTrigger[] | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowOption[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [list, wf] = await Promise.all([webhooks.list(), listWorkflows().catch(() => [])]);
      setItems(list);
      setWorkflows(wf.map((w) => ({ id: w.id, name: w.name })));
      setError(null);
    } catch (reason: unknown) {
      setError(message(reason, "Could not load webhook triggers"));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const nameOf = (id: string) => workflows.find((w) => w.id === id)?.name ?? id;
  const replace = (t: WebhookTrigger) => setItems((current) => (current ?? []).some((i) => i.id === t.id) ? (current ?? []).map((i) => (i.id === t.id ? { ...i, ...t } : i)) : [t, ...(current ?? [])]);
  const opened = items?.find((i) => i.id === openId) ?? null;

  return (
    <>
      <PageHeader crumbs={[navCrumb("webhooks", { current: true })]} actions={
        <Can kind="tenant" action="write">
          <Button variant="primary" onClick={() => setCreating(true)} data-testid="webhook-create"><Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />Create trigger</Button>
        </Can>
      } />
      <div className="mx-auto w-full max-w-5xl space-y-4 p-4" data-testid="webhooks-page">
        <p className={hintClass}>An outside system starts a published workflow with an HMAC-signed request. Each trigger is created disabled and enabled by a person.</p>
        {error ? <p role="alert" className={alertClass} data-testid="webhooks-error">{error}</p> : null}
        {items === null && !error ? <p className={hintClass}>Loading…</p> : null}
        {items !== null && items.length === 0 ? (
          <EmptyState title="No webhook triggers" description="Create a trigger to let GitHub, a payment provider or your own service start a workflow." testId="webhooks-empty" />
        ) : null}
        {items && items.length > 0 ? (
          <ul className="divide-y divide-border rounded-md border border-border" data-testid="webhook-list">
            {items.map((t) => (
              <li key={t.id} className="flex min-w-0 flex-wrap items-center justify-between gap-2 px-3 py-2.5" data-testid="webhook-row" data-trigger={t.id}>
                <div className="flex min-w-0 flex-col gap-0.5">
                  <button type="button" onClick={() => setOpenId(t.id)} className="min-w-0 break-words text-left text-label font-medium text-foreground underline-offset-2 hover:underline">{t.name}</button>
                  <span className="text-caption text-muted-foreground">{PRESET_LABELS[t.preset]} → {nameOf(t.workflow_id)} · last delivery {webhookWhen(t.last_delivery_at)}</span>
                </div>
                <Badge variant={STATUS_VARIANT[t.status]}>{STATUS_LABELS[t.status]}</Badge>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      {creating ? <CreateDialog workflows={workflows} onCreated={replace} onClose={() => setCreating(false)} /> : null}
      {opened ? <Detail trigger={opened} workflowName={nameOf(opened.workflow_id)} onChanged={replace} onClose={() => setOpenId(null)} /> : null}
    </>
  );
}
