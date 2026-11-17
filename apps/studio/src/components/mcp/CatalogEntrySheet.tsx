"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetFooter, SheetHeader } from "@/components/ui/Sheet";
import { ApiError } from "@/lib/api";
import { mcpCatalog, packageLabel, type CatalogEntry, type CatalogInstall } from "@/lib/mcpCatalog";
import type { McpServer } from "@/lib/mcpServers";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-body text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

type Choice = { kind: "package"; index: number } | { kind: "remote"; index: number };

/** A readable message for the install failures the API names (409 name taken, 422 refused input). */
export function describeInstallError(reason: unknown): string {
  if (reason instanceof ApiError) {
    if (reason.status === 409) return "Another server already uses that name. Choose a different name.";
    if (reason.status === 404) return "This catalog entry no longer exists.";
    if (reason.status === 422) return `The catalog entry could not be run as configured: ${reason.message}`;
    return reason.message;
  }
  return reason instanceof Error ? reason.message : "The server could not be created";
}

interface CatalogEntrySheetProps {
  entry: CatalogEntry | null;
  onClose: () => void;
  onInstalled: (server: McpServer) => void;
}

export function CatalogEntrySheet({ entry, onClose, onInstalled }: CatalogEntrySheetProps) {
  return (
    <Sheet open={entry !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      {entry ? (
        <SheetContent size="md" data-testid="mcp-catalog-sheet">
          <EntryForm entry={entry} onInstalled={onInstalled} />
        </SheetContent>
      ) : null}
    </Sheet>
  );
}

function EntryForm({ entry, onInstalled }: { entry: CatalogEntry; onInstalled: (server: McpServer) => void }) {
  const firstChoice: Choice | null = entry.packages.length > 0 ? { kind: "package", index: 0 } : entry.remotes.length > 0 ? { kind: "remote", index: 0 } : null;
  const [choice, setChoice] = useState<Choice | null>(firstChoice);
  const [name, setName] = useState("");
  const [env, setEnv] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEnv({});
    setError(null);
  }, [choice?.kind, choice?.index]);

  const envVars = useMemo(() => (choice?.kind === "package" ? entry.packages[choice.index]?.environment_variables ?? [] : []), [choice, entry]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || !choice) return;
    const missing = envVars.filter((variable) => variable.required && !(env[variable.name] ?? "").trim());
    if (missing.length > 0) {
      setError(`Enter a value for ${missing.map((variable) => variable.name).join(", ")}.`);
      return;
    }
    const body: CatalogInstall = { catalog_name: entry.name };
    if (choice.kind === "package") body.package_index = choice.index;
    else body.remote_index = choice.index;
    if (name.trim()) body.name = name.trim();
    if (choice.kind === "package") {
      const filled = Object.fromEntries(envVars.filter((variable) => (env[variable.name] ?? "") !== "").map((variable) => [variable.name, env[variable.name]]));
      if (Object.keys(filled).length > 0) body.env = filled;
    }
    setBusy(true);
    setError(null);
    try {
      onInstalled(await mcpCatalog.install(body));
    } catch (reason: unknown) {
      setError(describeInstallError(reason));
      setBusy(false);
    }
  };

  const optionClass = "flex min-w-0 items-start gap-2 rounded-md border border-border p-2 text-body";
  const runLabel = choice?.kind === "package" ? "Run server" : "Add server";

  return (
    <form noValidate onSubmit={submit} className="flex min-h-0 flex-1 flex-col" aria-label={`Run ${entry.title || entry.name}`}>
      <SheetHeader title={entry.title || entry.name} description={entry.name} />
      <SheetBody className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center gap-2">
          {entry.local ? <Badge variant="info">Local</Badge> : null}
          {entry.remote ? <Badge variant="outline">Remote</Badge> : null}
          {entry.status ? <Badge variant={entry.status === "active" ? "success" : "warning"}>{entry.status}</Badge> : null}
          {entry.version ? <span className="text-caption text-muted-foreground">Version {entry.version}</span> : null}
          {entry.licence ? <span className="text-caption text-muted-foreground">{entry.licence}</span> : null}
        </div>
        {entry.description ? <p className="text-body text-foreground">{entry.description}</p> : null}

        {firstChoice === null ? (
          <p className="text-body text-muted-foreground">This entry lists no package or remote endpoint to run.</p>
        ) : (
          <fieldset className="flex min-w-0 flex-col gap-2">
            <legend className="mb-1 text-label font-medium text-foreground">How to run it</legend>
            {entry.packages.map((pkg, index) => (
              <label key={`p${index}`} className={optionClass}>
                <input
                  type="radio"
                  name="run-choice"
                  checked={choice?.kind === "package" && choice.index === index}
                  onChange={() => setChoice({ kind: "package", index })}
                  className="mt-1"
                />
                <span className="min-w-0 break-all">
                  <span className="font-mono">{packageLabel(pkg)}</span>
                  <span className="block text-caption text-muted-foreground">Runs here{pkg.transport ? `, ${pkg.transport}` : ""}</span>
                </span>
              </label>
            ))}
            {entry.remotes.map((remote, index) => (
              <label key={`r${index}`} className={optionClass}>
                <input
                  type="radio"
                  name="run-choice"
                  checked={choice?.kind === "remote" && choice.index === index}
                  onChange={() => setChoice({ kind: "remote", index })}
                  className="mt-1"
                />
                <span className="min-w-0 break-all">
                  <span className="font-mono">{remote.url}</span>
                  <span className="block text-caption text-muted-foreground">Hosted elsewhere, {remote.type}</span>
                </span>
              </label>
            ))}
          </fieldset>
        )}

        {envVars.length > 0 ? (
          <fieldset className="flex min-w-0 flex-col gap-3">
            <legend className="mb-1 text-label font-medium text-foreground">Environment</legend>
            {envVars.map((variable) => (
              <div key={variable.name} className="flex min-w-0 flex-col gap-1">
                <label htmlFor={`env-${variable.name}`} className="flex flex-wrap items-center gap-2 text-label text-foreground">
                  <span className="font-mono">{variable.name}</span>
                  {variable.required ? <Badge variant="neutral">required</Badge> : <span className="text-caption text-muted-foreground">optional</span>}
                  {variable.secret ? <Badge variant="warning">secret</Badge> : null}
                </label>
                <input
                  id={`env-${variable.name}`}
                  type={variable.secret ? "password" : "text"}
                  autoComplete="off"
                  required={variable.required}
                  value={env[variable.name] ?? ""}
                  onChange={(event) => setEnv((current) => ({ ...current, [variable.name]: event.target.value }))}
                  className={inputClass}
                  aria-describedby={variable.description ? `env-${variable.name}-help` : undefined}
                />
                {variable.description ? <span id={`env-${variable.name}-help`} className="text-caption text-muted-foreground">{variable.description}</span> : null}
              </div>
            ))}
            <p className="text-caption text-muted-foreground">Secret values are stored sealed and cannot be read back.</p>
          </fieldset>
        ) : null}

        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor="catalog-server-name" className="text-label font-medium text-foreground">Server name</label>
          <input id="catalog-server-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Optional; defaults to the catalog name" className={inputClass} />
        </div>

        {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{error}</p> : null}
      </SheetBody>
      <SheetFooter>
        <Button type="submit" variant="primary" disabled={busy || !choice}>{busy ? "Working…" : runLabel}</Button>
      </SheetFooter>
    </form>
  );
}
