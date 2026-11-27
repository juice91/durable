"use client";

import React, { useCallback, useEffect, useId, useState } from "react";
import { CopyField } from "@/components/apiKeys/CopyField";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { externalAccess, type A2AEndpoint, type ExternalSettings } from "@/lib/externalAccess";
import { useCan } from "@/lib/permissions";
import { alertClass, fieldClass, hintClass, labelClass, message } from "./styles";

/** The tenant's A2A endpoint: the URL an A2A agent connects to, and what its public Agent Card says. */
export function A2AEndpointSection({ settings }: { settings: ExternalSettings | null }) {
  const id = useId();
  const canManage = useCan("tenant", null, "manage_external_access");
  const [endpoint, setEndpoint] = useState<A2AEndpoint | null | undefined>(undefined);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [rotating, setRotating] = useState(false);

  const show = useCallback((next: A2AEndpoint | null) => {
    setEndpoint(next);
    setName(next?.card_name ?? "");
    setDescription(next?.card_description ?? "");
  }, []);
  useEffect(() => {
    externalAccess.a2aEndpoint().then(show).catch((reason: unknown) => setError(message(reason, "Could not load the A2A endpoint")));
  }, [show]);

  const run = async (action: () => Promise<A2AEndpoint>) => {
    setBusy(true);
    setError(null);
    try {
      show(await action());
    } catch (reason: unknown) {
      setError(message(reason, "The change was refused"));
    } finally {
      setBusy(false);
      setRotating(false);
    }
  };

  const save = (enabled: boolean) => run(() => externalAccess.saveA2aEndpoint({ card_name: name.trim(), card_description: description.trim(), enabled }));
  const complete = Boolean(name.trim() && description.trim());

  return (
    <section aria-labelledby="external-a2a-title" className="space-y-2" data-testid="external-a2a">
      <h2 id="external-a2a-title" className="text-label font-semibold text-foreground">A2A endpoint</h2>
      {error ? <p role="alert" className={alertClass}>{error}</p> : null}
      {endpoint === undefined ? (error ? null : <p className={hintClass}>Loading…</p>) : (
        <div className="min-w-0 space-y-3 rounded-md border border-border p-3">
          {endpoint ? (
            <>
              <Badge variant={endpoint.enabled ? "success" : "neutral"} data-testid="external-a2a-status">{endpoint.enabled ? "Enabled" : "Disabled"}</Badge>
              {endpoint.url ? (
                <CopyField label="Endpoint URL" value={endpoint.url} copiedMessage="URL copied" testId="external-a2a-url" hint="Give an A2A agent this URL and a run key. It reads the Agent Card under it; exports with the A2A protocol are its skills." />
              ) : <p className={hintClass}>The URL needs EAP_EXTERNAL_BASE_URL.</p>}
            </>
          ) : <p className={hintClass}>No endpoint yet. Agents that speak A2A need one.</p>}
          {canManage ? (
            <form onSubmit={(event) => { event.preventDefault(); void save(endpoint?.enabled ?? false); }} className="min-w-0 space-y-3" aria-label="A2A Agent Card">
              <div className="space-y-1">
                <label htmlFor={`${id}-name`} className={labelClass}>Card name</label>
                <input id={`${id}-name`} data-testid="external-a2a-name" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} className={fieldClass} aria-describedby={`${id}-public`} />
              </div>
              <div className="space-y-1">
                <label htmlFor={`${id}-description`} className={labelClass}>Card description</label>
                <textarea id={`${id}-description`} data-testid="external-a2a-description" value={description} maxLength={2000} rows={2} onChange={(e) => setDescription(e.target.value)} className={fieldClass} aria-describedby={`${id}-public`} />
              </div>
              <p id={`${id}-public`} className={hintClass}>Anyone with the URL reads these two without a key: name no customer or process. No workflow is listed there.</p>
              <div className="flex flex-wrap gap-2">
                <Button variant="primary" size="sm" type="submit" disabled={busy || !complete} data-testid="external-a2a-save">{endpoint ? "Save card" : "Create endpoint"}</Button>
                {endpoint ? (
                  <>
                    <Button variant="secondary" size="sm" type="button" disabled={busy || !complete || (!endpoint.enabled && !settings?.base_url)} onClick={() => void save(!endpoint.enabled)} data-testid="external-a2a-toggle">
                      {endpoint.enabled ? "Disable endpoint" : "Enable endpoint"}
                    </Button>
                    <Button variant="secondary" size="sm" type="button" disabled={busy} onClick={() => setRotating(true)} data-testid="external-a2a-rotate">Rotate URL</Button>
                  </>
                ) : null}
              </div>
            </form>
          ) : null}
        </div>
      )}
      {rotating ? (
        <ConfirmDialog title="Rotate the endpoint URL?" description="The old URL stops answering at once, and every A2A agent needs the new one. Keys and runs stay." confirmLabel="Rotate" destructive busy={busy} onCancel={() => setRotating(false)}
          onConfirm={() => void run(() => externalAccess.rotateA2aEndpoint())} />
      ) : null}
    </section>
  );
}
