"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetFooter, SheetHeader } from "@/components/ui/Sheet";
import { toast } from "@/components/ui/Toaster";
import { formatWhen } from "@/components/knowledge/knowledgeUi";
import {
  CHECK_CODE_TEXT,
  KbSourceError,
  API_TOKEN_KIND,
  listAdapters,
  checkConnection,
  checkText,
  connectionPayload,
  createConnection,
  deleteConnection,
  listConnections,
  providerInfo,
  providerLabel as adapterLabel,
  sourcesUsingConnection,
  updateConnection,
  type AdapterInfo,
  type CheckResult,
  type Connection,
  type ConnectionForm,
} from "@/lib/kbSources";
import { listSecrets } from "@/lib/secrets";
import { ConflictNotice } from "@/components/shared/ConflictNotice";
import { useConflict } from "@/lib/useConflict";
import { SecretPicker } from "./SecretPicker";
import { Select } from "@/components/ui/Select";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function providerLabel(adapters: AdapterInfo[], connection: Connection): string {
  const provider = String(connection.config.provider ?? "");
  return provider ? adapterLabel(adapters, provider) : "Unknown provider";
}

function emptyForm(adapters: AdapterInfo[]): ConnectionForm {
  const provider = adapters[0];
  return { name: "", provider: provider?.provider ?? "", baseUrl: provider?.base_url ?? "", tokenUrl: provider?.token_url ?? "", clientIdSecret: "", clientSecretSecret: "", tokenSecret: "" };
}

function refName(ref: string | undefined): string {
  return ref?.startsWith("secret:") ? ref.slice("secret:".length) : "";
}

function formFor(connection: Connection): ConnectionForm {
  return {
    name: connection.name,
    provider: String(connection.config.provider ?? ""),
    baseUrl: connection.base_url ?? "",
    tokenUrl: typeof connection.config.token_url === "string" ? connection.config.token_url : "",
    clientIdSecret: refName(connection.credential_refs.client_id),
    clientSecretSecret: refName(connection.credential_refs.client_secret),
    tokenSecret: refName(connection.credential_refs.token),
  };
}

/** One option per provider (a provider with several source kinds lists once); the edited connection's own provider stays selectable. */
function providerOptions(adapters: AdapterInfo[], current: string): AdapterInfo[] {
  const seen = new Set<string>();
  const list = adapters.filter((item) => (seen.has(item.provider) ? false : (seen.add(item.provider), true)));
  if (current && !seen.has(current)) list.push({ kind: "", provider: current, distribution: "", display_name: current, connection_kind: "", base_url: null, token_url: null, config_schema: null });
  return list;
}

function StatusChip({ connection }: { connection: Connection }) {
  if (!connection.enabled) return <Badge variant="neutral">Disabled</Badge>;
  if (connection.last_status === "ok") return <Badge variant="success">Ok</Badge>;
  if (connection.last_status === "error") return <Badge variant="destructive">Error</Badge>;
  return <Badge variant="outline">Not checked</Badge>;
}

function ConnectionSheet({ editing, adapters, onClose, onSaved }: { editing: Connection | null; adapters: AdapterInfo[]; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState<ConnectionForm>(() => (editing ? formFor(editing) : emptyForm(adapters)));
  const [names, setNames] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 412: the connection changed elsewhere; 重新載入 refills the form from their version, 覆寫 saves on top of it.
  const conflict = useConflict<Connection>({ onSaved: () => finishSave() });
  const finishSave = () => {
    toast.success(editing ? "Connection saved" : "Connection added");
    onSaved();
    onClose();
  };

  const refreshNames = useCallback(() => {
    listSecrets().then(
      (items) => setNames(items.map((item) => item.name)),
      () => undefined,
    );
  }, []);
  useEffect(refreshNames, [refreshNames]);

  const set = (patch: Partial<ConnectionForm>) => setForm((current) => ({ ...current, ...patch }));
  const apiToken = providerInfo(adapters, form.provider)?.connection_kind === API_TOKEN_KIND;
  // An API-token connection's token is optional (a public repository needs none); OAuth needs both secrets.
  const valid = form.name.trim() !== "" && form.baseUrl.trim() !== "" && (apiToken || (form.clientIdSecret !== "" && form.clientSecretSecret !== ""));
  const chooseProvider = (value: string) => {
    const info = providerInfo(adapters, value);
    set({ provider: value, baseUrl: info?.base_url ?? "", tokenUrl: info?.token_url ?? "", clientIdSecret: "", clientSecretSecret: "", tokenSecret: "" });
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || !valid) return;
    setBusy(true);
    setError(null);
    try {
      const body = connectionPayload(form, editing?.enabled ?? true, adapters);
      if (editing) {
        const saved = await conflict.guard(() =>
          updateConnection(editing.id, { name: body.name, base_url: body.base_url, config: body.config, credential_refs: body.credential_refs }));
        if (saved === undefined) return; // held as a conflict notice
      } else {
        await createConnection(body);
      }
      finishSave();
    } catch (reason: unknown) {
      setError(errorText(reason, "The connection could not be saved"));
    } finally {
      setBusy(false);
    }
  };

  const verb = editing ? "Save changes" : "Add connection";
  return (
    <Sheet open onOpenChange={(next) => { if (!next && !busy) onClose(); }}>
      <SheetContent size="md" data-testid="connection-sheet">
        <SheetHeader title={editing ? `Edit ${editing.name}` : "Add connection"} description="Credentials are chosen from stored secrets; only their names are saved with the connection." />
        <form onSubmit={submit} className="flex min-h-0 min-w-0 flex-1 flex-col">
          <SheetBody className="flex flex-col gap-4">
            <div className="space-y-1">
              <label htmlFor="connection-name" className="block text-sm font-medium text-foreground">Name</label>
              <input id="connection-name" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={100} autoComplete="off" className={inputClass} />
            </div>
            <div className="space-y-1">
              <label htmlFor="connection-provider" className="block text-sm font-medium text-foreground">Provider</label>
              <Select id="connection-provider" value={form.provider} onValueChange={(v) => chooseProvider(v)} disabled={!!editing} className={inputClass}>
                {providerOptions(adapters, form.provider).map((provider) => (
                  <option key={provider.provider} value={provider.provider}>{provider.display_name}</option>
                ))}
              </Select>
            </div>
            <div className="space-y-1">
              <label htmlFor="connection-base-url" className="block text-sm font-medium text-foreground">Base URL</label>
              <input id="connection-base-url" value={form.baseUrl} onChange={(e) => set({ baseUrl: e.target.value })} inputMode="url" autoComplete="off" spellCheck={false} className={`${inputClass} font-mono`} />
            </div>
            {apiToken ? (
              <>
                <SecretPicker label="Token" names={names} value={form.tokenSecret} onChange={(name) => set({ tokenSecret: name })} onCreated={refreshNames} />
                <p className="-mt-2 text-caption text-muted-foreground">Optional. Without a token only public repositories can be read, under the provider&apos;s lower unauthenticated rate limit.</p>
              </>
            ) : (
              <>
                <div className="space-y-1">
                  <label htmlFor="connection-token-url" className="block text-sm font-medium text-foreground">Token URL</label>
                  <input id="connection-token-url" value={form.tokenUrl} onChange={(e) => set({ tokenUrl: e.target.value })} inputMode="url" autoComplete="off" spellCheck={false} className={`${inputClass} font-mono`} />
                </div>
                <SecretPicker label="Client ID" names={names} value={form.clientIdSecret} onChange={(name) => set({ clientIdSecret: name })} onCreated={refreshNames} />
                <SecretPicker label="Client secret" names={names} value={form.clientSecretSecret} onChange={(name) => set({ clientSecretSecret: name })} onCreated={refreshNames} />
              </>
            )}
            <ConflictNotice state={conflict} onReload={(current) => current && setForm(formFor(current))} />
            {error ? (
              <p role="alert" data-testid="connection-form-error" className="text-label text-destructive-text">{error}</p>
            ) : null}
          </SheetBody>
          <SheetFooter>
            <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={busy || !valid}>{verb}</Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}

/**
 * Settings → Connections: tenant connections to knowledge-base source providers. A connection holds
 * `secret:<name>` references to stored secrets, never a value; Check calls the provider and shows a
 * stable result code.
 */
export function ConnectionsSection() {
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adapters, setAdapters] = useState<AdapterInfo[] | null>(null);
  useEffect(() => {
    listAdapters().then(setAdapters, () => setAdapters([]));
  }, []);
  const [sheet, setSheet] = useState<{ editing: Connection | null } | null>(null);
  const [checks, setChecks] = useState<Record<string, { busy: boolean; result?: CheckResult; error?: string }>>({});
  const [status, setStatus] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Connection | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const listed = await listConnections();
      if (seq !== loadSeq.current) return;
      setConnections(listed);
      setLoadError(null);
    } catch (reason: unknown) {
      if (seq === loadSeq.current) setLoadError(errorText(reason, "Could not load connections"));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const check = async (connection: Connection) => {
    setChecks((all) => ({ ...all, [connection.id]: { busy: true } }));
    try {
      const result = await checkConnection(connection.id);
      setChecks((all) => ({ ...all, [connection.id]: { busy: false, result } }));
      void load();
    } catch (reason: unknown) {
      setChecks((all) => ({ ...all, [connection.id]: { busy: false, error: errorText(reason, "The check could not run") } }));
    }
  };

  const toggleConflict = useConflict<Connection>({ onSaved: () => void load() });
  const toggle = async (connection: Connection) => {
    try {
      const saved = await toggleConflict.guard(() => updateConnection(connection.id, { enabled: !connection.enabled }));
      if (saved === undefined) return; // held: the notice offers reload or overwrite
      setStatus(`Connection ${connection.name} ${connection.enabled ? "disabled" : "enabled"}.`);
      void load();
    } catch (reason: unknown) {
      setStatus(errorText(reason, "The connection could not be changed"));
    }
  };

  const remove = async (connection: Connection) => {
    if (deleteBusy) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteConnection(connection.id);
      setDeleting(null);
      setStatus(`Connection ${connection.name} deleted.`);
      toast.success("Connection deleted");
      void load();
    } catch (reason: unknown) {
      if (reason instanceof KbSourceError && reason.code === "connection_in_use") {
        const users = await sourcesUsingConnection(connection.id).catch(() => []);
        const names = users.map((source) => `${source.name} (${source.knowledgeBaseName})`).join(", ");
        setDeleteError(names ? `Still used by ${names}. Change or remove those sources first.` : "A source still uses this connection. Change or remove it first.");
      } else if (reason instanceof KbSourceError && reason.code === "connection_not_found") {
        setDeleting(null);
        setStatus(`Connection ${connection.name} no longer exists.`);
        void load();
      } else {
        setDeleteError(errorText(reason, "The connection could not be deleted"));
      }
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <section id="connections" aria-labelledby="settings-connections" data-testid="settings-connections" className="flex min-w-0 scroll-mt-4 flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 id="settings-connections" className="text-title font-semibold text-foreground">Connections</h2>
          <p className="text-label text-muted-foreground">
            Provider accounts that knowledge-base sources sync from. A connection stores the names of secrets, never their values.
          </p>
        </div>
        <Button variant="primary" size="sm" onClick={() => setSheet({ editing: null })}>Add connection</Button>
      </div>
      {loadError ? (
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <p role="alert" data-testid="connections-error" className="text-label text-destructive-text">{loadError}</p>
          <Button variant="secondary" size="sm" onClick={() => void load()}>Retry</Button>
        </div>
      ) : connections === null ? (
        <p className="text-label text-muted-foreground">Loading connections…</p>
      ) : connections.length === 0 ? (
        <p data-testid="connections-empty" className="text-label text-muted-foreground">No connections yet.</p>
      ) : (
        <ul aria-label="Connections" data-testid="connections-list" className="min-w-0">
          {connections.map((connection) => {
            const state = checks[connection.id];
            const stored = connection.last_status === "error" ? CHECK_CODE_TEXT[connection.last_error_code ?? ""] ?? `The check failed (${connection.last_error_code ?? "unknown"}).` : null;
            return (
              <li key={connection.id} data-connection={connection.id} className="flex min-w-0 flex-col gap-1.5 border-b border-border py-2 last:border-b-0">
                <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                  <span className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
                    <span className="break-all text-label font-medium text-foreground">{connection.name}</span>
                    <span className="text-caption text-muted-foreground">{providerLabel(adapters ?? [], connection)}</span>
                    <StatusChip connection={connection} />
                  </span>
                  <span className="flex flex-wrap gap-1.5">
                    <Button variant="secondary" size="sm" aria-label={`Check ${connection.name}`} disabled={state?.busy} onClick={() => void check(connection)}>
                      {state?.busy ? "Checking…" : "Check"}
                    </Button>
                    <Button variant="secondary" size="sm" aria-label={`Edit ${connection.name}`} onClick={() => setSheet({ editing: connection })}>Edit</Button>
                    <Button variant="secondary" size="sm" aria-label={`${connection.enabled ? "Disable" : "Enable"} ${connection.name}`} onClick={() => void toggle(connection)}>
                      {connection.enabled ? "Disable" : "Enable"}
                    </Button>
                    <Button variant="secondary" size="sm" aria-label={`Delete ${connection.name}`} onClick={() => { setDeleteError(null); setDeleting(connection); }}>Delete</Button>
                  </span>
                </div>
                <p className="text-caption text-muted-foreground">
                  {connection.last_checked_at ? `Checked ${formatWhen(connection.last_checked_at)}` : "Never checked"}
                  {stored ? ` · ${stored}` : ""}
                </p>
                {state?.result || state?.error ? (
                  <p role="status" data-testid="connection-check" className={`text-label ${state.result?.ok ? "text-success-text" : "text-destructive-text"}`}>
                    {state.error ?? (state.result ? checkText(state.result) : "")}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      <ConflictNotice state={toggleConflict} onReload={() => void load()} />
      <p role="status" data-testid="connections-status" className="min-h-5 text-label text-muted-foreground">{status}</p>
      {sheet && adapters ? <ConnectionSheet editing={sheet.editing} adapters={adapters} onClose={() => setSheet(null)} onSaved={() => void load()} /> : null}
      {deleting ? (
        <ConfirmDialog
          title={`Delete ${deleting.name}?`}
          description="Sources can no longer sync through this connection. This cannot be undone."
          confirmLabel="Delete"
          destructive
          busy={deleteBusy}
          error={deleteError}
          onConfirm={() => void remove(deleting)}
          onCancel={() => setDeleting(null)}
        />
      ) : null}
    </section>
  );
}
