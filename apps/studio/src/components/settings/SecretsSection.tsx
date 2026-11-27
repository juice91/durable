"use client";

import { useCan, usePermissions } from "@/lib/permissions";
import { CopyButton } from "@/components/ui/CopyButton";
import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { PermissionNotice } from "@/components/access/PermissionNotice";
import { ResourceAccessPanel } from "@/components/access/ResourceAccessPanel";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { toast } from "@/components/ui/Toaster";
import { ApiError } from "@/lib/api";
import { routes } from "@/lib/routes";
import { deleteSecret, listSecrets, putSecret, secretRef, type SecretInfo } from "@/lib/secrets";
import { useAccessEnabled } from "@/lib/useIam";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/** What "why can't I?" asks about on the secrets list. */
const SECRET_ACTIONS = ["create"] as const;

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

/** A secret row's actions: Access for who may change access (an admin), Delete for who may delete this secret. */
function SecretActions({ secret, accessEnabled, onAccess, onDelete, onEditable }: { secret: SecretInfo; accessEnabled: boolean; onAccess: () => void; onDelete: () => void; onEditable: () => void }) {
  const permissions = usePermissions("secret", secret.name, ["archive", "edit"]);
  const manages = useCan("tenant", null, "manage_settings");
  const editable = !permissions.loading && permissions.can("edit");
  useEffect(() => {
    if (editable) onEditable();
  }, [editable, onEditable]);
  const used = (secret.used_by ?? []).length > 0;
  return (
    <>
      {accessEnabled && manages ? (
        <Button variant="ghost" size="sm" aria-label={`Access to ${secret.name}`} onClick={onAccess}>
          Access
        </Button>
      ) : null}
      {permissions.can("archive") ? (
        <Button variant="secondary" size="sm" disabled={used} title={used ? "Change the servers using this secret first" : undefined} aria-label={`Delete ${secret.name}`} onClick={onDelete}>
          Delete
        </Button>
      ) : null}
    </>
  );
}

/**
 * Settings → Secrets: the tenant's secret names (values are write-only and never shown), Add
 * secret with a masked value, and Delete after a confirm. Agents and MCP headers refer to a
 * secret as `secret:<name>`.
 */
export function SecretsSection() {
  const [secrets, setSecrets] = useState<SecretInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const accessEnabled = useAccessEnabled();
  const mayCreate = useCan("secret", null, "create");
  // An owner who may not create secrets may still replace a value of their own.
  const [editsOne, setEditsOne] = useState(false);
  const markEditable = useCallback(() => setEditsOne(true), []);
  const [accessOf, setAccessOf] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const listed = await listSecrets();
      if (seq !== loadSeq.current) return;
      setSecrets(listed);
      setLoadError(null);
    } catch (reason: unknown) {
      if (seq === loadSeq.current) setLoadError(errorText(reason, "Could not load secrets"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const add = async (event: React.FormEvent) => {
    event.preventDefault();
    const trimmed = name.trim();
    if (saving || !trimmed || !value) return;
    setSaving(true);
    setFormError(null);
    try {
      const { restarted } = await putSecret(trimmed, value);
      // Clear the typed pair and reset the DOM form as well: a browser password manager may have
      // autofilled the inputs behind React, and the value must never linger on screen.
      setName("");
      setValue("");
      formRef.current?.reset();
      const restart = restarted.length > 0 ? ` Restarted ${restarted.length} ${restarted.length === 1 ? "server" : "servers"} to pick it up.` : "";
      setStatus(`Secret ${trimmed} saved. Use it as ${secretRef(trimmed)}.${restart}`);
      toast.success("Secret saved");
      void load();
    } catch (reason: unknown) {
      // The message is built from the response, never from what was typed.
      setFormError(errorText(reason, "The secret could not be saved"));
      setValue("");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (secretName: string) => {
    if (deleteBusy) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteSecret(secretName);
      setDeleting(null);
      setStatus(`Secret ${secretName} deleted.`);
      toast.success("Secret deleted");
      void load();
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 404) {
        setDeleting(null);
        setStatus(`Secret ${secretName} no longer exists.`);
        void load();
      } else if (reason instanceof ApiError && reason.status === 409) {
        setDeleting(null);
        setStatus(`Secret ${secretName} was not deleted. ${reason.message}`);
        void load();
      } else {
        setDeleteError(errorText(reason, "The secret could not be deleted"));
      }
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <section id="secrets" aria-labelledby="settings-secrets" data-testid="settings-secrets" className="flex min-w-0 scroll-mt-4 flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex min-w-0 flex-col gap-1">
        <h2 id="settings-secrets" className="text-title font-semibold text-foreground">
          Secrets
        </h2>
        <p className="text-label text-muted-foreground">
          Credentials agents and MCP servers can use without ever seeing them. Values are stored encrypted and never shown again; refer to one as <code>secret:&lt;name&gt;</code>.
        </p>
      </div>
      {loadError ? (
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <p role="alert" data-testid="secrets-error" className="text-label text-destructive-text">
            {loadError}
          </p>
          <Button variant="secondary" size="sm" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      ) : secrets === null ? (
        <p className="text-label text-muted-foreground">Loading secrets…</p>
      ) : secrets.length === 0 ? (
        <p data-testid="secrets-empty" className="text-label text-muted-foreground">
          No secrets yet.
        </p>
      ) : (
        <ul aria-label="Secrets" data-testid="secrets-list" className="min-w-0">
          {secrets.map((secret) => (
            <li key={secret.name} data-secret={secret.name} className="flex min-w-0 flex-wrap items-center justify-between gap-2 border-b border-border py-2 last:border-b-0">
              <span className="min-w-0 flex-1">
                <span className="flex min-w-0 items-center gap-1">
                  <span className="select-text break-all font-mono text-label text-foreground">{secret.name}</span>
                  <CopyButton text={secret.name} label="secret name" />
                </span>
                {(secret.used_by ?? []).length > 0 ? (
                  <span data-testid={`secret-used-by-${secret.name}`} className="block text-caption text-muted-foreground">
                    Used by{" "}
                    {(secret.used_by ?? []).map((user, index) => (
                      <React.Fragment key={user.server_id}>
                        {index > 0 ? ", " : null}
                        <Link href={routes.mcpServer(user.server_id)} className="text-foreground underline underline-offset-4 hover:no-underline">
                          {user.name}
                        </Link>
                      </React.Fragment>
                    ))}
                  </span>
                ) : null}
              </span>
              <SecretActions
                secret={secret}
                accessEnabled={accessEnabled}
                onAccess={() => setAccessOf(secret.name)}
                onEditable={markEditable}
                onDelete={() => {
                  setDeleteError(null);
                  setDeleting(secret.name);
                }}
              />
            </li>
          ))}
        </ul>
      )}
      <PermissionNotice kind="secret" id={null} actions={SECRET_ACTIONS} />
      {mayCreate || editsOne ? (
        <form ref={formRef} onSubmit={add} aria-label="Add secret" data-testid="secrets-form" className="flex min-w-0 flex-col gap-3 border-t border-border pt-3">
          <div className="grid min-w-0 gap-3 md:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor="secret-name" className="block text-sm font-medium text-foreground">
                Secret name
              </label>
              <input id="secret-name" name="secret-ref-name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" data-1p-ignore data-lpignore="true" data-form-type="other" spellCheck={false} placeholder="slack-token" className={`${inputClass} font-mono`} />
            </div>
            <div className="space-y-1">
              <label htmlFor="secret-value" className="block text-sm font-medium text-foreground">
                Secret value
              </label>
              <input id="secret-value" name="secret-ref-value" type="password" value={value} onChange={(e) => setValue(e.target.value)} autoComplete="new-password" data-1p-ignore data-lpignore="true" data-form-type="other" spellCheck={false} className={`${inputClass} font-mono`} />
            </div>
          </div>
          {formError ? (
            <p role="alert" data-testid="secrets-form-error" className="text-label text-destructive-text">
              {formError}
            </p>
          ) : null}
          <div>
            <Button type="submit" variant="primary" size="sm" disabled={saving || !name.trim() || !value}>
              Add secret
            </Button>
          </div>
        </form>
      ) : null}
      <p role="status" data-testid="secrets-status" className="min-h-5 text-label text-muted-foreground">
        {status}
      </p>
      {accessOf ? (
        <Dialog open onOpenChange={(open) => (open ? undefined : setAccessOf(null))}>
          <DialogContent className="max-w-xl">
            <DialogHeader>
              <DialogTitle>Access to {accessOf}</DialogTitle>
              <DialogDescription>Who holds which relation on this secret. Its value is never shown.</DialogDescription>
            </DialogHeader>
            <ResourceAccessPanel kind="secret" id={accessOf} />
          </DialogContent>
        </Dialog>
      ) : null}
      {deleting ? (
        <ConfirmDialog
          title={`Delete ${deleting}?`}
          description="Anything that refers to this secret stops working until it is added again. This cannot be undone."
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
