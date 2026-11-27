"use client";

import { PermissionNotice } from "@/components/access/PermissionNotice";
import { useCan } from "@/lib/permissions";
import React, { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetFooter, SheetHeader } from "@/components/ui/Sheet";
import { ApiError } from "@/lib/api";
import { mcpCatalog, networkSummary, packageLabel, type CatalogEntry, type CatalogInstall } from "@/lib/mcpCatalog";
import type { McpServer } from "@/lib/mcpServers";
import { listSecrets, putSecret, secretRef } from "@/lib/secrets";
import { Select } from "@/components/ui/Select";

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

/** Suggested stored-secret name: `<server-slug>-<var>`, lowercase, dashes for anything else. */
export function suggestSecretName(catalogName: string, variable: string): string {
  const clean = (text: string) => text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const slug = clean(catalogName.split("/").pop() ?? catalogName);
  return [slug, clean(variable)].filter(Boolean).join("-");
}

type SecretSource = "new" | "stored";

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

/** What "why can't I?" asks about here. */
const CREATE_ACTIONS = ["create"] as const;

function EntryForm({ entry, onInstalled }: { entry: CatalogEntry; onInstalled: (server: McpServer) => void }) {
  const mayCreate = useCan("mcp_server", null, "create");
  const firstChoice: Choice | null = entry.packages.length > 0 ? { kind: "package", index: 0 } : entry.remotes.length > 0 ? { kind: "remote", index: 0 } : null;
  const [choice, setChoice] = useState<Choice | null>(firstChoice);
  const [name, setName] = useState("");
  const [env, setEnv] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Secret variables: where the value comes from, the stored name picked, and the name a new value is saved under.
  const [source, setSource] = useState<Record<string, SecretSource>>({});
  const [stored, setStored] = useState<Record<string, string>>({});
  const [newNames, setNewNames] = useState<Record<string, string>>({});
  const [storedNames, setStoredNames] = useState<string[]>([]);

  useEffect(() => {
    let live = true;
    listSecrets()
      .then((items) => live && setStoredNames(items.map((item) => item.name)))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    setEnv({});
    setSource({});
    setStored({});
    setNewNames({});
    setError(null);
  }, [choice?.kind, choice?.index]);

  const envVars = useMemo(() => (choice?.kind === "package" ? entry.packages[choice.index]?.environment_variables ?? [] : []), [choice, entry]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || !choice) return;
    const isStored = (variable: (typeof envVars)[number]) => variable.secret && source[variable.name] === "stored";
    const given = (variable: (typeof envVars)[number]) => (isStored(variable) ? Boolean(stored[variable.name]) : (env[variable.name] ?? "").trim() !== "");
    const missing = envVars.filter((variable) => variable.required && !given(variable));
    if (missing.length > 0) {
      setError(`${missing.some(isStored) ? "Enter or choose" : "Enter"} a value for ${missing.map((variable) => variable.name).join(", ")}.`);
      return;
    }
    const nameFor = (variable: (typeof envVars)[number]) => (newNames[variable.name] ?? suggestSecretName(entry.name, variable.name)).trim();
    const fresh = envVars.filter((variable) => variable.secret && !isStored(variable) && (env[variable.name] ?? "") !== "");
    const taken = fresh.find((variable) => storedNames.includes(nameFor(variable)));
    if (taken) {
      setError(`A secret named ${nameFor(taken)} already exists. Choose it under "Use a stored secret", or give this one another name.`);
      return;
    }
    const body: CatalogInstall = { catalog_name: entry.name };
    if (choice.kind === "package") body.package_index = choice.index;
    else body.remote_index = choice.index;
    if (name.trim()) body.name = name.trim();
    setBusy(true);
    setError(null);
    try {
      if (choice.kind === "package") {
        // Secret values are stored first and only their refs go in the start body; a raw secret is never sent with it.
        const filled: Record<string, string> = {};
        for (const variable of envVars) {
          if (isStored(variable)) filled[variable.name] = secretRef(stored[variable.name]);
          else if ((env[variable.name] ?? "") !== "") {
            if (variable.secret) {
              await putSecret(nameFor(variable), env[variable.name]);
              filled[variable.name] = secretRef(nameFor(variable));
            } else filled[variable.name] = env[variable.name];
          }
        }
        if (Object.keys(filled).length > 0) body.env = filled;
      }
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
          {entry.source === "toolhive" ? <Badge variant="info">ToolHive</Badge> : null}
          {entry.source === "toolhive" && entry.tier ? <Badge variant="outline">{entry.tier}</Badge> : null}
          {entry.local ? <Badge variant="info">Local</Badge> : null}
          {entry.remote ? <Badge variant="outline">Remote</Badge> : null}
          {entry.status ? <Badge variant={entry.status === "active" ? "success" : "warning"}>{entry.status}</Badge> : null}
          {entry.version ? <span className="text-caption text-muted-foreground">Version {entry.version}</span> : null}
          {entry.licence ? <span className="text-caption text-muted-foreground">{entry.licence}</span> : null}
        </div>
        {entry.description ? <p className="text-body text-foreground">{entry.description}</p> : null}
        {entry.source === "toolhive" ? (
          <div className="flex min-w-0 flex-col gap-1 text-caption text-muted-foreground">
            <span>{entry.tool_count} {entry.tool_count === 1 ? "tool" : "tools"}</span>
            {networkSummary(entry.network) ? <span>Network: {networkSummary(entry.network)}</span> : null}
            {entry.tags.length > 0 ? (
              <span className="flex flex-wrap gap-1">{entry.tags.map((tag) => <Badge key={tag} variant="neutral">{tag}</Badge>)}</span>
            ) : null}
          </div>
        ) : null}

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
            {envVars.map((variable) => {
              const fromStore = variable.secret && source[variable.name] === "stored";
              return (
              <div key={variable.name} className="flex min-w-0 flex-col gap-1">
                <label htmlFor={`env-${variable.name}`} className="flex flex-wrap items-center gap-2 text-label text-foreground">
                  <span className="font-mono">{variable.name}</span>
                  {variable.required ? <Badge variant="neutral">required</Badge> : <span className="text-caption text-muted-foreground">optional</span>}
                  {variable.secret ? <Badge variant="warning">secret</Badge> : null}
                </label>
                {variable.secret ? (
                  <Select
                    aria-label={`Source for ${variable.name}`}
                    value={source[variable.name] ?? "new"}
                    onValueChange={(v) => setSource((current) => ({ ...current, [variable.name]: v as SecretSource }))}
                    className={inputClass}
                  >
                    <option value="new">Enter a new value</option>
                    <option value="stored" disabled={storedNames.length === 0}>Use a stored secret</option>
                  </Select>
                ) : null}
                {fromStore ? (
                  <Select
                    aria-label={`Stored secret for ${variable.name}`}
                    value={stored[variable.name] ?? ""}
                    onValueChange={(v) => setStored((current) => ({ ...current, [variable.name]: v }))}
                    className={inputClass}
                  >
                    <option value="">Choose a secret…</option>
                    {storedNames.map((storedName) => <option key={storedName} value={storedName}>{storedName}</option>)}
                  </Select>
                ) : (
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
                )}
                {variable.secret && !fromStore ? (
                  <input
                    aria-label={`Secret name for ${variable.name}`}
                    autoComplete="off"
                    spellCheck={false}
                    value={newNames[variable.name] ?? suggestSecretName(entry.name, variable.name)}
                    onChange={(event) => setNewNames((current) => ({ ...current, [variable.name]: event.target.value }))}
                    className={`${inputClass} font-mono`}
                  />
                ) : null}
                {variable.description ? <span id={`env-${variable.name}-help`} className="text-caption text-muted-foreground">{variable.description}</span> : null}
              </div>
              );
            })}
            <p className="text-caption text-muted-foreground">A new secret value is stored encrypted under the name shown and cannot be read back; the server gets a reference to it.</p>
          </fieldset>
        ) : null}

        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor="catalog-server-name" className="text-label font-medium text-foreground">Server name</label>
          <input id="catalog-server-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Optional; defaults to the catalog name" className={inputClass} />
        </div>

        {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{error}</p> : null}
      </SheetBody>
      <SheetFooter>
        {mayCreate ? (
          <Button type="submit" variant="primary" disabled={busy || !choice}>{busy ? "Working…" : runLabel}</Button>
        ) : (
          <PermissionNotice kind="mcp_server" id={null} actions={CREATE_ACTIONS} />
        )}
      </SheetFooter>
    </form>
  );
}
