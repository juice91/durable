"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { CopyField } from "@/components/apiKeys/CopyField";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import type { IamMe } from "@/lib/iam";
import {
  daysUntil,
  EXPIRY_PRESET_DAYS,
  expiryFromDays,
  isPersonSession,
  MAX_EXPIRY_DAYS,
  PERSONAL_SCOPES,
  personalApiKeys,
  personalKeyErrorMessage,
  scopeDisabledReason,
  type PersonalApiKey,
  type PersonalApiKeyCreated,
  type PersonalScope,
} from "@/lib/personalApiKeys";

const fieldClass =
  "w-full min-w-0 rounded-md border border-input bg-surface px-3 py-1.5 text-label text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const labelClass = "block text-label font-medium text-foreground";
const hintClass = "text-caption text-muted-foreground";
const alertClass = "rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text";

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "Never");

function statusVariant(status: PersonalApiKey["status"]) {
  return status === "active" ? "success" : status === "expired" ? "warning" : "destructive";
}

interface CreateProps {
  roles: string[];
  onCreated: (key: PersonalApiKey) => void;
  onClose: () => void;
}

/** The secret lives in this dialog's state only; closing it unmounts the state and nothing can show it again. */
function CreateKeyDialog({ roles, onCreated, onClose }: CreateProps) {
  const id = useId();
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<PersonalScope[]>(["mcp"]);
  const [preset, setPreset] = useState<number | "custom">(90);
  const [customDate, setCustomDate] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<PersonalApiKeyCreated | null>(null);
  const secretRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (created) secretRef.current?.querySelector<HTMLElement>("[data-copy-button]")?.focus();
  }, [created]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    if (!name.trim()) return setError("Give the key a name.");
    if (scopes.length === 0) return setError("Pick at least one scope.");
    let expiresAt: string;
    if (preset === "custom") {
      const date = customDate ? new Date(`${customDate}T23:59:59`) : null;
      if (!date || Number.isNaN(date.getTime())) return setError("Pick an expiry date.");
      const days = daysUntil(date);
      if (days < 1) return setError("The expiry must be in the future.");
      if (days > MAX_EXPIRY_DAYS) return setError(`A key can last at most ${MAX_EXPIRY_DAYS} days.`);
      expiresAt = date.toISOString();
    } else {
      // A hair under the cap so clock skew between here and the server cannot make 365 days "too far".
      expiresAt = expiryFromDays(preset, new Date(Date.now() - 60_000));
    }
    setSaving(true);
    setError(null);
    try {
      const result = await personalApiKeys.create({ name: name.trim(), scopes, expires_at: expiresAt });
      setCreated(result);
      const { key: _secret, ...listed } = result;
      void _secret;
      onCreated(listed);
    } catch (reason: unknown) {
      setError(personalKeyErrorMessage(reason));
    } finally {
      setSaving(false);
    }
  };

  const maxDate = new Date(Date.now() + MAX_EXPIRY_DAYS * 86_400_000).toISOString().slice(0, 10);
  const minDate = new Date(Date.now() + 86_400_000).toISOString().slice(0, 10);

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
      <DialogContent data-testid="personal-key-dialog">
        {created ? (
          <>
            <DialogHeader>
              <DialogTitle>Copy your new key</DialogTitle>
              <DialogDescription data-testid="personal-key-once">
                You won&apos;t see this key again. Copy it now and store it somewhere safe; if you lose it, revoke it and create another.
              </DialogDescription>
            </DialogHeader>
            <div ref={secretRef} data-testid="personal-key-secret">
              <CopyField label={`${created.name} key`} value={created.key} copiedMessage="Key copied" />
            </div>
            <DialogFooter>
              <Button variant="primary" onClick={onClose} data-testid="personal-key-done">Done</Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Create a personal API key</DialogTitle>
              <DialogDescription>The key acts as you and never has more access than your roles allow. It always expires.</DialogDescription>
            </DialogHeader>
            <form onSubmit={(event) => void submit(event)} aria-label="Create a personal API key" className="min-w-0 space-y-4" data-testid="personal-key-form">
              <div className="space-y-1">
                <label htmlFor={`${id}-name`} className={labelClass}>Name</label>
                <input id={`${id}-name`} data-testid="personal-key-name" value={name} maxLength={100} required onChange={(e) => setName(e.target.value)} className={fieldClass} placeholder="Laptop MCP client" />
              </div>
              <fieldset className="min-w-0 space-y-1.5">
                <legend className={labelClass}>Scopes</legend>
                {PERSONAL_SCOPES.map(({ scope, label, hint }) => {
                  const reason = scopeDisabledReason(scope, roles);
                  return (
                    <div key={scope}>
                      <label className="flex items-center gap-2 text-label text-foreground">
                        <input
                          type="checkbox"
                          data-testid={`personal-key-scope-${scope}`}
                          checked={scopes.includes(scope)}
                          disabled={reason !== null}
                          aria-describedby={`${id}-${scope}`}
                          onChange={(e) => setScopes(e.target.checked ? [...scopes, scope] : scopes.filter((s) => s !== scope))}
                        />
                        <span className="font-mono">{label}</span>
                      </label>
                      <p id={`${id}-${scope}`} className={hintClass} data-testid={reason ? `personal-key-scope-reason-${scope}` : undefined}>{reason ?? hint}</p>
                    </div>
                  );
                })}
              </fieldset>
              <fieldset className="min-w-0 space-y-1.5">
                <legend className={labelClass}>Expires</legend>
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {EXPIRY_PRESET_DAYS.map((days) => (
                    <label key={days} className="flex items-center gap-2 text-label text-foreground">
                      <input type="radio" name={`${id}-expiry`} checked={preset === days} onChange={() => setPreset(days)} data-testid={`personal-key-expiry-${days}`} />
                      {days} days
                    </label>
                  ))}
                  <label className="flex items-center gap-2 text-label text-foreground">
                    <input type="radio" name={`${id}-expiry`} checked={preset === "custom"} onChange={() => setPreset("custom")} data-testid="personal-key-expiry-custom" />
                    Custom date
                  </label>
                </div>
                {preset === "custom" ? (
                  <div className="space-y-1">
                    <label htmlFor={`${id}-date`} className="sr-only">Expiry date</label>
                    <input id={`${id}-date`} type="date" data-testid="personal-key-expiry-date" value={customDate} min={minDate} max={maxDate} onChange={(e) => setCustomDate(e.target.value)} className={fieldClass} />
                    <p className={hintClass}>At most {MAX_EXPIRY_DAYS} days from today.</p>
                  </div>
                ) : null}
              </fieldset>
              {error ? <p role="alert" className={alertClass} data-testid="personal-key-error">{error}</p> : null}
              <DialogFooter>
                <Button type="button" variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
                <Button type="submit" variant="primary" disabled={saving} data-testid="personal-key-submit">{saving ? "Creating…" : "Create key"}</Button>
              </DialogFooter>
            </form>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** Profile → API keys: the signed-in person's own keys. Hidden (with one line why) for a session that is not a person. */
export function PersonalApiKeys({ me }: { me: IamMe }) {
  const person = isPersonSession(me);
  const [keys, setKeys] = useState<PersonalApiKey[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<PersonalApiKey | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoadError(null);
    personalApiKeys.list().then(setKeys).catch((reason: unknown) => setLoadError(personalKeyErrorMessage(reason)));
  }, []);
  useEffect(() => {
    if (person) load();
  }, [person, load]);

  const heading = (
    <div className="flex min-w-0 flex-col gap-1">
      <h2 id="profile-api-keys" className="text-title font-semibold text-foreground">API keys</h2>
      <p className="text-label text-muted-foreground">Keys of your own for MCP clients and scripts. They act as you, are capped by your roles and always expire.</p>
    </div>
  );

  if (!person) {
    return (
      <section aria-labelledby="profile-api-keys" data-testid="profile-api-keys" className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-4">
        {heading}
        <p data-testid="profile-api-keys-unavailable" className="text-label text-muted-foreground">
          Personal API keys need a signed-in person. This is a dev or key session, so sign in with your own account to manage them.
        </p>
      </section>
    );
  }

  return (
    <section aria-labelledby="profile-api-keys" data-testid="profile-api-keys" className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
        {heading}
        <Button variant="primary" size="sm" onClick={() => setCreating(true)} data-testid="personal-key-create">Create key</Button>
      </div>
      {loadError ? (
        <div className="flex min-w-0 flex-col items-start gap-2">
          <p role="alert" className={alertClass} data-testid="personal-keys-error">{loadError}</p>
          <Button variant="secondary" size="sm" onClick={load}>Retry</Button>
        </div>
      ) : keys === null ? (
        <p role="status" className="text-label text-muted-foreground">Loading…</p>
      ) : keys.length === 0 ? (
        <p data-testid="personal-keys-empty" className="text-label text-muted-foreground">You have no API keys yet.</p>
      ) : (
        <div className="min-w-0 overflow-x-auto">
          <table className="w-full md:min-w-[40rem] text-left text-label" data-testid="personal-keys-table">
            <thead className="text-muted-foreground">
              <tr>
                {["Name", "Prefix", "Scopes", "Created", "Last used", "Expires", "Status", ""].map((h, i) => (
                  <th key={i} scope="col" className="py-1 pr-3 font-medium">{h || <span className="sr-only">Actions</span>}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {keys.map((key) => (
                <tr key={key.id} data-testid={`personal-key-row-${key.id}`}>
                  <td className="py-1.5 pr-3 text-foreground">{key.name}</td>
                  <td className="py-1.5 pr-3 font-mono">{key.prefix}</td>
                  <td className="py-1.5 pr-3 font-mono">{key.scopes.join(", ")}</td>
                  <td className="py-1.5 pr-3">{when(key.created_at)}</td>
                  <td className="py-1.5 pr-3">{when(key.last_used_at)}</td>
                  <td className="py-1.5 pr-3">{when(key.expires_at)}</td>
                  <td className="py-1.5 pr-3"><Badge variant={statusVariant(key.status)}>{key.status}</Badge></td>
                  <td className="py-1.5 text-right">
                    {key.status === "active" ? (
                      <Button variant="secondary" size="sm" onClick={() => { setRevokeError(null); setRevoking(key); }} data-testid={`personal-key-revoke-${key.id}`}>
                        Revoke<span className="sr-only"> {key.name}</span>
                      </Button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {creating ? (
        <CreateKeyDialog roles={me.roles} onCreated={(key) => setKeys((current) => [key, ...(current ?? [])])} onClose={() => setCreating(false)} />
      ) : null}
      {revoking ? (
        <ConfirmDialog
          title={`Revoke ${revoking.name}?`}
          description="Anything using this key stops working at once. This cannot be undone."
          confirmLabel="Revoke"
          destructive
          busy={revokeBusy}
          error={revokeError}
          onCancel={() => setRevoking(null)}
          onConfirm={() => {
            const target = revoking;
            setRevokeBusy(true);
            setRevokeError(null);
            personalApiKeys
              .revoke(target.id)
              .then(() => {
                setRevoking(null);
                setKeys((current) => (current ?? []).map((k) => (k.id === target.id ? { ...k, status: "revoked", revoked_at: new Date().toISOString() } : k)));
              })
              .catch((reason: unknown) => setRevokeError(personalKeyErrorMessage(reason)))
              .finally(() => setRevokeBusy(false));
          }}
        />
      ) : null}
    </section>
  );
}
