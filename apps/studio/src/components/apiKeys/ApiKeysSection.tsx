"use client";

import { CopyButton } from "@/components/ui/CopyButton";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { ApiError } from "@/lib/api";
import { API_KEY_STATUS_LABELS, apiKeys, describeAccess, hasManageScope, hasOtlpScope, type ApiKey, type ApiKeyStatus } from "@/lib/apiKeys";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Badge, type BadgeProps } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { toast } from "@/components/ui/Toaster";
import { ApiKeyCreateDialog } from "./ApiKeyCreateDialog";
import { McpConnectPanel } from "./McpConnectPanel";
import { OtlpConnectPanel } from "./OtlpConnectPanel";

/** The section's element id: Connect (AG-UI) links to `/settings#api-keys`. */
export const API_KEYS_SECTION_ID = "api-keys";

const STATUS_VARIANT: Record<ApiKeyStatus, BadgeProps["variant"]> = { active: "success", revoked: "neutral", expired: "warning" };

function when(iso: string | null): string {
  if (!iso) return "Never";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString();
}

/** The API's 401 when it does not run with EAP_AUTH_MODE=dev (management takes the dev bearer only). */
const DEV_AUTH_DISABLED = "dev bearer authentication is not enabled";
export const API_KEYS_DEV_ONLY_MESSAGE = "API keys can be managed only when the API runs in dev mode (EAP_AUTH_MODE=dev).";

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function KeyRow({ apiKey, onRevoke }: { apiKey: ApiKey; onRevoke: (key: ApiKey) => void }) {
  return (
    <li data-testid="api-key-row" data-api-key={apiKey.id} className="flex min-w-0 flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-border py-2.5 last:border-b-0">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="min-w-0 break-words text-label font-medium text-foreground">{apiKey.name}</span>
          <Badge variant={STATUS_VARIANT[apiKey.status]} data-testid="api-key-status">
            {API_KEY_STATUS_LABELS[apiKey.status]}
          </Badge>
          {hasOtlpScope(apiKey) ? (
            <Badge variant="info" data-testid="api-key-otlp">
              OTLP
            </Badge>
          ) : null}
          {hasManageScope(apiKey) ? (
            <Badge variant="info" data-testid="api-key-manage">
              Manage
            </Badge>
          ) : null}
        </div>
        <span className="flex min-w-0 items-center gap-1">
          <span className="min-w-0 break-all font-mono text-caption text-muted-foreground" data-testid="api-key-prefix">
          {apiKey.prefix}…
        </span>
          <CopyButton text={apiKey.prefix} label="key prefix" />
        </span>
        <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-caption">
          <dt className="text-muted-foreground">Access</dt>
          <dd className="min-w-0 break-words text-foreground">{describeAccess(apiKey)}</dd>
          {apiKey.allowed_origins && apiKey.allowed_origins.length > 0 ? (
            <>
              <dt className="text-muted-foreground">Origins</dt>
              <dd className="min-w-0 break-all text-foreground">{apiKey.allowed_origins.join(", ")}</dd>
            </>
          ) : null}
          <dt className="text-muted-foreground">Created</dt>
          <dd className="min-w-0 text-foreground">{when(apiKey.created_at)}</dd>
          <dt className="text-muted-foreground">Last used</dt>
          <dd className="min-w-0 text-foreground">{when(apiKey.last_used_at)}</dd>
          {apiKey.expires_at ? (
            <>
              <dt className="text-muted-foreground">Expires</dt>
              <dd className="min-w-0 text-foreground">{when(apiKey.expires_at)}</dd>
            </>
          ) : null}
          {apiKey.revoked_at ? (
            <>
              <dt className="text-muted-foreground">Revoked</dt>
              <dd className="min-w-0 text-foreground">{when(apiKey.revoked_at)}</dd>
            </>
          ) : null}
        </dl>
      </div>
      {apiKey.status === "active" ? (
        <Button variant="secondary" size="sm" onClick={() => onRevoke(apiKey)} aria-label={`Revoke ${apiKey.name}`} data-testid="api-key-revoke">
          Revoke
        </Button>
      ) : null}
    </li>
  );
}

/**
 * Settings → API keys (AGUI.3 routes, D16): the tenant's keys, newest first, with Create and
 * Revoke. A new key's plaintext is shown once, in the create dialog; this list never has it.
 */
export function ApiKeysSection() {
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Management is refused in this deployment (not dev mode): retrying cannot help.
  const [devOnly, setDevOnly] = useState(false);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Set when the dialog closes because the key was revoked: its Revoke button is about to go.
  const revokedRef = useRef(false);

  // Only the latest list request may write the list: an older answer (a slow first load, or the
  // reload after an earlier revoke) would otherwise hide a key just created or revoke state.
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const listed = await apiKeys.list();
      if (seq !== loadSeq.current) return;
      setKeys(listed);
      setLoadError(null);
      setDevOnly(false);
    } catch (reason: unknown) {
      if (seq !== loadSeq.current) return;
      const refused = reason instanceof ApiError && reason.status === 401 && reason.message === DEV_AUTH_DISABLED;
      setDevOnly(refused);
      setLoadError(refused ? API_KEYS_DEV_ONLY_MESSAGE : errorText(reason, "Could not load API keys"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onCreated = (key: ApiKey) => {
    setKeys((current) => [key, ...(current ?? []).filter((item) => item.id !== key.id)]);
    setStatus(`Key ${key.name} created.`);
    // Supersede any list request that started before the key existed, and fetch one that has it.
    void load();
  };

  const revoke = async (key: ApiKey) => {
    if (revokeBusy) return;
    setRevokeBusy(true);
    setRevokeError(null);
    try {
      await apiKeys.revoke(key.id);
      revokedRef.current = true;
      setRevoking(null);
      setStatus(`Key ${key.name} revoked. Requests that use it are refused from now on.`);
      toast.success("Key revoked");
      // Not awaited: the busy state covers the DELETE only, so another key can be revoked while
      // this reload runs (the latest reload wins, see loadSeq).
      void load();
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 404) {
        revokedRef.current = true;
        setRevoking(null);
        setStatus(`Key ${key.name} no longer exists.`);
        void load();
      } else {
        setRevokeError(errorText(reason, "The key could not be revoked"));
      }
    } finally {
      setRevokeBusy(false);
    }
  };

  return (
    <section
      id={API_KEYS_SECTION_ID}
      aria-labelledby="settings-api-keys"
      data-testid="settings-api-keys"
      className="flex min-w-0 scroll-mt-4 flex-col gap-3 rounded-lg border border-border p-4"
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <h2 id="settings-api-keys" ref={headingRef} tabIndex={-1} className="text-title font-semibold text-foreground outline-none">
            API keys
          </h2>
          <p className="text-label text-muted-foreground">
            Keys let third-party frontends (CopilotKit, any AG-UI client) run your published agents and workflows, and let MCP clients manage this workspace. Use them from a server, not a browser.
          </p>
        </div>
        {devOnly ? null : (
          <Button variant="primary" size="sm" onClick={() => setCreating(true)} data-testid="api-key-create">
            <Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            Create key
          </Button>
        )}
      </div>
      {loadError ? (
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <p role="alert" className="text-label text-destructive-text" data-testid="api-keys-error">
            {loadError}
          </p>
          {devOnly ? null : (
            <Button variant="secondary" size="sm" onClick={() => void load()}>
              Retry
            </Button>
          )}
        </div>
      ) : keys === null ? (
        <p className="text-label text-muted-foreground">Loading keys…</p>
      ) : keys.length === 0 ? (
        <p className="text-label text-muted-foreground" data-testid="api-keys-empty">
          No keys yet.
        </p>
      ) : (
        <ul aria-label="API keys" className="min-w-0" data-testid="api-keys-list">
          {keys.map((key) => (
            <KeyRow
              key={key.id}
              apiKey={key}
              onRevoke={(item) => {
                setRevokeError(null);
                revokedRef.current = false;
                setRevoking(item);
              }}
            />
          ))}
        </ul>
      )}
      <p role="status" className="min-h-5 text-label text-muted-foreground" data-testid="api-keys-status">
        {status}
      </p>
      {devOnly ? null : <McpConnectPanel />}
      {devOnly ? null : <OtlpConnectPanel />}
      {creating ? <ApiKeyCreateDialog onCreated={onCreated} onClose={() => setCreating(false)} /> : null}
      {revoking ? (
        <ConfirmDialog
          title={`Revoke ${revoking.name}?`}
          description="Every request that uses this key is refused from now on, and open streams close within a minute. This cannot be undone."
          confirmLabel="Revoke"
          destructive
          busy={revokeBusy}
          error={revokeError}
          onConfirm={() => void revoke(revoking)}
          onCancel={() => setRevoking(null)}
          fallbackFocus={() => headingRef.current}
          skipOpenerReturn={() => revokedRef.current}
        />
      ) : null}
    </section>
  );
}
