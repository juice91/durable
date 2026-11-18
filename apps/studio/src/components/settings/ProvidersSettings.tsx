"use client";

import React, { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { fetchProviders, PROVIDER_STATE_LABELS, PROVIDER_STATE_VARIANTS, testProvider, type ProviderStatus } from "@/lib/providers";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";

function checkedText(iso: string | null): string {
  if (!iso) return "Not checked";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : `Checked ${new Date(t).toLocaleTimeString()}`;
}

function ProviderRow({ provider, testing, onTest }: { provider: ProviderStatus; testing: boolean; onTest: (id: string) => void }) {
  return (
    <li data-testid="provider-row" data-provider={provider.id} className="flex min-w-0 flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-border py-3 last:border-b-0">
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h2 className="min-w-0 break-words text-label font-semibold text-foreground">{provider.label}</h2>
          <span className="font-mono text-caption text-muted-foreground">{provider.id}</span>
          <Badge variant={PROVIDER_STATE_VARIANTS[provider.status]} data-testid="provider-status">
            {PROVIDER_STATE_LABELS[provider.status]}
          </Badge>
        </div>
        <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-caption">
          <dt className="text-muted-foreground">Models</dt>
          <dd className="text-foreground" data-testid="provider-model-count">{provider.status === "ok" ? provider.model_count : "None listed"}</dd>
          <dt className="text-muted-foreground">API key variable</dt>
          <dd className="min-w-0 break-all font-mono text-foreground" data-testid="provider-env-var">{provider.env_var}</dd>
          <dt className="text-muted-foreground">Base URL host</dt>
          <dd className="min-w-0 break-all font-mono text-foreground" data-testid="provider-host">{provider.base_url_host ?? "Not set"}</dd>
        </dl>
        {provider.error ? <p className="min-w-0 break-words text-caption text-muted-foreground" data-testid="provider-error">{provider.error}</p> : null}
        <p className="text-caption text-muted-foreground">{checkedText(provider.checked_at)}</p>
      </div>
      <Button variant="secondary" size="sm" disabled={testing} onClick={() => onTest(provider.id)} aria-label={`Test connection to ${provider.label}`} data-testid="provider-test">
        <RefreshCw strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        {testing ? "Testing…" : "Test connection"}
      </Button>
    </li>
  );
}

/**
 * Settings → Providers: the model providers the server can reach, with their status, model count,
 * key variable name and base URL host, and a Test connection button. Read-only: the keys come from
 * the server's environment, never through Studio, and the API never returns one.
 */
export function ProvidersSettings({ embedded = false }: { embedded?: boolean } = {}) {
  const [providers, setProviders] = useState<ProviderStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    return fetchProviders().then(setProviders, (reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not load the providers"));
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const test = (id: string) => {
    setTesting(id);
    setTestError(null);
    testProvider(id)
      .then((next) => setProviders((current) => (current ?? []).map((p) => (p.id === next.id ? next : p))))
      .catch((reason: unknown) => setTestError(reason instanceof Error ? reason.message : "The connection test failed"))
      .finally(() => setTesting(null));
  };

  return (
    <div className={embedded ? "flex min-w-0 flex-col gap-4" : "mx-auto flex w-full min-w-0 max-w-3xl flex-col gap-4 px-4 py-6"} data-testid="providers-settings">
      <div className="flex min-w-0 flex-col gap-1">
        <p className="text-label text-muted-foreground" data-testid="providers-readonly-note">
          Read-only. Provider keys come from the server&apos;s environment (deployment secrets); Studio never shows or stores them. Which models an agent may use is set by the platform allowlist (EAP_ALLOWED_MODELS).
        </p>
      </div>
      {error ? (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-label text-destructive-text">
          <span className="min-w-0 break-words">{error}</span>
          <Button size="sm" onClick={() => void load()}>Retry</Button>
        </div>
      ) : null}
      {testError ? <p role="alert" className="text-label text-destructive-text">{testError}</p> : null}
      {providers === null && !error ? <p className="text-label text-muted-foreground">Loading providers…</p> : null}
      {providers ? (
        <ul className="rounded-lg border border-border px-4" data-testid="provider-list">
          {providers.map((provider) => (
            <ProviderRow key={provider.id} provider={provider} testing={testing === provider.id} onTest={test} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}
