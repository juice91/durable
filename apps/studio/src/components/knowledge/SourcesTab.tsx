"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetFooter, SheetHeader } from "@/components/ui/Sheet";
import { toast } from "@/components/ui/Toaster";
import {
  MAX_TARGETED_PARTS,
  listAdapters,
  REPOSITORY_PATTERN,
  REPOSITORY_SOURCE_KIND,
  SOURCE_KIND,
  SYNC_MODES,
  createSource,
  findSyncWorkflow,
  getSync,
  listConnections,
  listSources,
  listSyncs,
  parseLines,
  parseMetadataMap,
  parsePartNumbers,
  providerInfo,
  providerLabel as adapterLabel,
  publishSyncWorkflow,
  repositoryConfig,
  reportSummary,
  startSourceSync,
  updateSource,
  type AdapterInfo,
  type Connection,
  type Source,
  type SyncDetail,
  type SyncMode,
  type SyncRow,
} from "@/lib/kbSources";
import { routes } from "@/lib/routes";
import { formatWhen } from "./knowledgeUi";
import { ConflictNotice } from "@/components/shared/ConflictNotice";
import { useConflict } from "@/lib/useConflict";
import { Select } from "@/components/ui/Select";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const TERMINAL = new Set(["completed", "failed", "needs_attention", "cancelled"]);
const MODE_LABEL: Record<SyncMode, string> = { targeted: "Targeted (part numbers)", incremental: "Incremental (new and changed)", full: "Full catalogue" };
const REPOSITORY_MODE_LABEL: Record<SyncMode, string> = { targeted: "Targeted (file paths)", incremental: "Incremental (changed files)", full: "Full repository" };
const MAP_PLACEHOLDER = "docs/api_guide/device/am263x/**: {sdk_family: mcu-plus-sdk, device: AM263x}\n\"**\": {sdk_family: mcu-plus-sdk}";

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function AddSourceSheet({ kbId, onClose, onAdded }: { kbId: string; onClose: () => void; onAdded: () => void }) {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [adapters, setAdapters] = useState<AdapterInfo[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [name, setName] = useState("");
  const [pageSize, setPageSize] = useState("100");
  const [extended, setExtended] = useState(false);
  const [inactive, setInactive] = useState(false);
  const [datasheets, setDatasheets] = useState(true);
  const [repository, setRepository] = useState("");
  const [ref, setRef] = useState("");
  const [include, setInclude] = useState("");
  const [exclude, setExclude] = useState("");
  const [mapText, setMapText] = useState("");
  const [deleteMissing, setDeleteMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listAdapters().then(setAdapters, () => undefined);
    listConnections().then(
      (items) => setConnections(items.filter((item) => item.enabled)),
      (reason: unknown) => setError(errorText(reason, "Could not load connections")),
    );
  }, []);

  const connection = connections.find((item) => item.id === connectionId);
  const sourceKind = providerInfo(adapters, String(connection?.config.provider ?? ""))?.kind ?? SOURCE_KIND;
  const repoSource = sourceKind === REPOSITORY_SOURCE_KIND;
  const size = Number(pageSize);
  const sizeOk = Number.isInteger(size) && size >= 1 && size <= 1000;
  const repositoryOk = REPOSITORY_PATTERN.test(repository.trim());
  const includeOk = parseLines(include).length > 0;
  const mapError = useMemo(() => {
    if (!repoSource) return null;
    try {
      parseMetadataMap(mapText);
      return null;
    } catch (reason: unknown) {
      return errorText(reason, "The map is not valid");
    }
  }, [repoSource, mapText]);
  const valid = !!connection && name.trim() !== "" && (repoSource ? repositoryOk && includeOk && mapError === null : sizeOk);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!connection || busy || !valid) return;
    setBusy(true);
    setError(null);
    try {
      await createSource(kbId, {
        connection_id: connection.id,
        name: name.trim(),
        kind: sourceKind,
        provider: String(connection.config.provider ?? ""),
        config: repoSource
          ? repositoryConfig({ repository, ref, include, exclude, map: mapText, deleteMissing })
          : { page_size: size, include_extended: extended, include_inactive: inactive, document_policy: { types: datasheets ? ["datasheet"] : [] } },
        enabled: true,
      });
      toast.success("Source added");
      onAdded();
      onClose();
    } catch (reason: unknown) {
      setError(errorText(reason, "The source could not be added"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet open onOpenChange={(next) => { if (!next && !busy) onClose(); }}>
      <SheetContent size="md" data-testid="source-sheet">
        <SheetHeader title="Add source" description="A source syncs a provider's catalogue or repository into this knowledge base." />
        <form onSubmit={submit} className="flex min-h-0 min-w-0 flex-1 flex-col">
          <SheetBody className="flex flex-col gap-4">
            <div className="space-y-1">
              <label htmlFor="source-connection" className="block text-sm font-medium text-foreground">Connection</label>
              <Select id="source-connection" value={connectionId} onValueChange={(v) => setConnectionId(v)} className={inputClass}>
                <option value="">Choose a connection…</option>
                {connections.map((item) => (
                  <option key={item.id} value={item.id}>{item.name}</option>
                ))}
              </Select>
              {connections.length === 0 ? <p className="text-caption text-muted-foreground">Add a connection in Settings first.</p> : null}
            </div>
            <div className="space-y-1">
              <label htmlFor="source-name" className="block text-sm font-medium text-foreground">Name</label>
              <input id="source-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} autoComplete="off" className={inputClass} />
            </div>
            {repoSource ? (
              <>
            <div className="space-y-1">
              <label htmlFor="source-repository" className="block text-sm font-medium text-foreground">Repository</label>
              <input id="source-repository" value={repository} onChange={(e) => setRepository(e.target.value)} placeholder="owner/name" autoComplete="off" spellCheck={false} className={`${inputClass} font-mono`} />
              {repository.trim() !== "" && !repositoryOk ? <p className="text-caption text-destructive-text">Use owner/name.</p> : null}
            </div>
            <div className="space-y-1">
              <label htmlFor="source-ref" className="block text-sm font-medium text-foreground">Ref</label>
              <input id="source-ref" value={ref} onChange={(e) => setRef(e.target.value)} placeholder="Default branch" autoComplete="off" spellCheck={false} className={`${inputClass} font-mono`} />
              <p className="text-caption text-muted-foreground">A branch, tag or commit; empty follows the default branch.</p>
            </div>
            <div className="space-y-1">
              <label htmlFor="source-include" className="block text-sm font-medium text-foreground">Include</label>
              <textarea id="source-include" value={include} onChange={(e) => setInclude(e.target.value)} rows={3} spellCheck={false} autoComplete="off" placeholder={"docs/**"} className={`${inputClass} font-mono`} />
              <p className="text-caption text-muted-foreground">Path globs, one per line (at least one). Supported types: md, rst, html, txt, yaml, json, pdf.</p>
            </div>
            <div className="space-y-1">
              <label htmlFor="source-exclude" className="block text-sm font-medium text-foreground">Exclude</label>
              <textarea id="source-exclude" value={exclude} onChange={(e) => setExclude(e.target.value)} rows={2} spellCheck={false} autoComplete="off" placeholder={"docs/old/**"} className={`${inputClass} font-mono`} />
            </div>
            <div className="space-y-1">
              <label htmlFor="source-map" className="block text-sm font-medium text-foreground">Metadata map (YAML)</label>
              <textarea id="source-map" value={mapText} onChange={(e) => setMapText(e.target.value)} rows={6} spellCheck={false} autoComplete="off" placeholder={MAP_PLACEHOLDER} aria-invalid={mapError !== null} aria-describedby="source-map-help" className={`${inputClass} font-mono`} />
              <p id="source-map-help" className={`text-caption ${mapError ? "text-destructive-text" : "text-muted-foreground"}`} role={mapError ? "alert" : undefined}>
                {mapError ?? "glob: {key: value}, first match wins; the path is added to every document."}
              </p>
            </div>
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-1 text-sm font-medium text-foreground">Policy</legend>
              <label className="flex items-start gap-2 text-sm text-foreground">
                <input type="checkbox" checked={deleteMissing} onChange={(e) => setDeleteMissing(e.target.checked)} className="mt-1" />
                <span>Delete documents whose files disappeared<span className="block text-caption text-muted-foreground">After a complete walk; refused beyond 50 documents or 20 %.</span></span>
              </label>
            </fieldset>
              </>
            ) : (
              <>
            <div className="space-y-1">
              <label htmlFor="source-page-size" className="block text-sm font-medium text-foreground">Page size</label>
              <input id="source-page-size" type="number" min={1} max={1000} value={pageSize} onChange={(e) => setPageSize(e.target.value)} className={inputClass} />
              {!sizeOk ? <p className="text-caption text-destructive-text">Use a whole number from 1 to 1000.</p> : null}
            </div>
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-1 text-sm font-medium text-foreground">Policy</legend>
              <label className="flex items-center gap-2 text-sm text-foreground"><input type="checkbox" checked={datasheets} onChange={(e) => setDatasheets(e.target.checked)} /> Queue datasheets for ingestion</label>
              <label className="flex items-center gap-2 text-sm text-foreground"><input type="checkbox" checked={extended} onChange={(e) => setExtended(e.target.checked)} /> Include extended data (parametrics, quality)</label>
              <label className="flex items-center gap-2 text-sm text-foreground"><input type="checkbox" checked={inactive} onChange={(e) => setInactive(e.target.checked)} /> Include inactive products</label>
            </fieldset>
              </>
            )}
            {error ? <p role="alert" className="text-label text-destructive-text">{error}</p> : null}
          </SheetBody>
          <SheetFooter>
            <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={busy || !valid}>Add source</Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}

function RunSyncSheet({ source, onClose, onStarted }: { source: Source; onClose: () => void; onStarted: (started: { workflowId: string; runId: string }) => void }) {
  const [mode, setMode] = useState<SyncMode>("targeted");
  const [text, setText] = useState("");
  const [restart, setRestart] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const repoSource = source.kind === REPOSITORY_SOURCE_KIND;
  const modeLabel = repoSource ? REPOSITORY_MODE_LABEL : MODE_LABEL;
  const noun = repoSource ? "file paths" : "part numbers";
  const parts = parsePartNumbers(text);
  const tooMany = parts.length > MAX_TARGETED_PARTS;
  const valid = mode === "targeted" ? parts.length > 0 && !tooMany : true;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || !valid) return;
    setBusy(true);
    setError(null);
    try {
      const started = await startSourceSync(source.id, mode, { partNumbers: mode === "targeted" ? parts : [], restart: mode === "full" && restart });
      onStarted(started);
    } catch (reason: unknown) {
      setError(errorText(reason, "The sync could not be started"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet open onOpenChange={(next) => { if (!next && !busy) onClose(); }}>
      <SheetContent size="md" data-testid="run-sync-sheet">
        <SheetHeader title={`Run sync: ${source.name}`} description="Starts a run of the KB Source Synchronization workflow." />
        <form onSubmit={submit} className="flex min-h-0 min-w-0 flex-1 flex-col">
          <SheetBody className="flex flex-col gap-4">
            <div className="space-y-1">
              <label htmlFor="sync-mode" className="block text-sm font-medium text-foreground">Mode</label>
              <Select id="sync-mode" value={mode} onValueChange={(v) => setMode(v as SyncMode)} className={inputClass}>
                {SYNC_MODES.map((item) => (
                  <option key={item} value={item}>{modeLabel[item]}</option>
                ))}
              </Select>
            </div>
            {mode === "targeted" ? (
              <div className="space-y-1">
                <label htmlFor="sync-parts" className="block text-sm font-medium text-foreground">{repoSource ? "File paths" : "Part numbers"}</label>
                <textarea id="sync-parts" value={text} onChange={(e) => setText(e.target.value)} rows={6} spellCheck={false} autoComplete="off" placeholder={repoSource ? "docs/guide/intro.md, one per line" : "One per line, or separated by commas"} className={`${inputClass} font-mono`} />
                <p className={`text-caption ${tooMany ? "text-destructive-text" : "text-muted-foreground"}`}>
                  {parts.length} of {MAX_TARGETED_PARTS} {noun}{tooMany ? `; use at most ${MAX_TARGETED_PARTS} per sync` : ""}
                </p>
              </div>
            ) : null}
            {mode === "full" ? (
              <label className="flex items-start gap-2 text-sm text-foreground">
                <input type="checkbox" checked={restart} onChange={(e) => setRestart(e.target.checked)} className="mt-1" />
                <span>Restart from the beginning<span className="block text-caption text-muted-foreground">{repoSource ? "Ignore the stored position and walk the repository again." : "Ignore the stored cursor and walk the catalogue again."}</span></span>
              </label>
            ) : null}
            {error ? <p role="alert" className="text-label text-destructive-text">{error}</p> : null}
          </SheetBody>
          <SheetFooter>
            <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={busy || !valid}>{busy ? "Starting…" : "Start sync"}</Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}

function SyncList({ kbId, sourceId, workflowId, refreshKey }: { kbId: string; sourceId: string; workflowId: string | null; refreshKey: number }) {
  const [syncs, setSyncs] = useState<SyncRow[] | null>(null);
  const [details, setDetails] = useState<Record<string, SyncDetail>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listSyncs(kbId, sourceId, 10).then(
      (rows) => {
        if (cancelled) return;
        setSyncs(rows);
        setError(null);
        rows.filter((row) => TERMINAL.has(row.status)).forEach((row) => {
          getSync(kbId, sourceId, row.run_id).then(
            (detail) => { if (!cancelled) setDetails((all) => ({ ...all, [row.run_id]: detail })); },
            () => undefined,
          );
        });
      },
      (reason: unknown) => { if (!cancelled) setError(errorText(reason, "Could not load the syncs")); },
    );
    return () => { cancelled = true; };
  }, [kbId, sourceId, refreshKey]);

  if (error) return <p role="alert" className="text-label text-destructive-text">{error}</p>;
  if (syncs === null) return <p className="text-label text-muted-foreground">Loading syncs…</p>;
  if (syncs.length === 0) return <p className="text-label text-muted-foreground">No syncs yet.</p>;
  return (
    <ul aria-label="Syncs" className="min-w-0 divide-y divide-border rounded-md border border-border">
      {syncs.map((sync) => {
        const detail = details[sync.run_id];
        const summary = reportSummary({ report: detail?.report, counters: sync.counters });
        const href = sync.parent_run_id && workflowId ? routes.workflowRun(workflowId, sync.parent_run_id) : routes.run(sync.run_id);
        return (
          <li key={sync.run_id} data-sync={sync.run_id} className="flex min-w-0 flex-col gap-1 px-3 py-2">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <Badge variant={sync.status === "completed" ? "success" : sync.status === "failed" || sync.status === "needs_attention" ? "destructive" : "info"}>{sync.status}</Badge>
              <span className="text-label text-foreground">{sync.mode}</span>
              <span className="text-caption text-muted-foreground">{formatWhen(sync.started_at)}</span>
              <Link href={href} className="ml-auto text-label font-medium text-foreground underline underline-offset-4 hover:no-underline" aria-label={`Run ${sync.run_id.slice(0, 8)}`}>
                Run
              </Link>
            </div>
            <p className="text-caption text-muted-foreground">
              discovered {summary.discovered} · inserted {summary.inserted} · updated {summary.updated} · unchanged {summary.unchanged} · documents queued {summary.documents_queued} · failed {summary.failed}
              {summary.stop_reason ? ` · stopped: ${summary.stop_reason}` : ""}
            </p>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * Knowledge base → Sources: the provider sources that fill this knowledge base. Run sync starts a
 * run of the published "KB Source Synchronization" workflow and links to it.
 */
export function SourcesTab({ kbId, kbName }: { kbId: string; kbName: string }) {
  const [sources, setSources] = useState<Source[] | null>(null);
  const [adapters, setAdapters] = useState<AdapterInfo[]>([]);
  useEffect(() => {
    listAdapters().then(setAdapters, () => undefined);
  }, []);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [workflowId, setWorkflowId] = useState<string | null>(null);
  const [workflowKnown, setWorkflowKnown] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [running, setRunning] = useState<Source | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [started, setStarted] = useState<{ workflowId: string; runId: string; sourceName: string } | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const ticket = ++seq.current;
    try {
      const listed = await listSources(kbId);
      if (ticket !== seq.current) return;
      setSources(listed);
      setLoadError(null);
    } catch (reason: unknown) {
      if (ticket === seq.current) setLoadError(errorText(reason, "Could not load sources"));
    }
  }, [kbId]);

  useEffect(() => {
    void load();
    return () => { seq.current += 1; };
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    findSyncWorkflow().then(
      (workflow) => { if (!cancelled) { setWorkflowId(workflow?.id ?? null); setWorkflowKnown(true); } },
      () => { if (!cancelled) setWorkflowKnown(false); },
    );
    return () => { cancelled = true; };
  }, []);

  const publish = async () => {
    setPublishing(true);
    setPublishError(null);
    try {
      const workflow = await publishSyncWorkflow();
      setWorkflowId(workflow.id);
      setWorkflowKnown(true);
      toast.success("Sync workflow published");
    } catch (reason: unknown) {
      setPublishError(errorText(reason, "The sync workflow could not be published"));
    } finally {
      setPublishing(false);
    }
  };

  // 412: the source changed elsewhere; 重新載入 shows their version, 覆寫 resends the toggle on top of it.
  const conflict = useConflict<Source>({ onSaved: () => void load() });
  const toggle = async (source: Source) => {
    try {
      const saved = await conflict.guard(() => updateSource(kbId, source.id, { enabled: !source.enabled }));
      if (saved === undefined) return; // held: the notice offers reload or overwrite
      setStatus(`Source ${source.name} ${source.enabled ? "disabled" : "enabled"}.`);
      void load();
    } catch (reason: unknown) {
      setStatus(errorText(reason, "The source could not be changed"));
    }
  };

  const toggleSyncs = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const runReady = workflowId !== null;
  return (
    <div data-testid="knowledge-sources" className="flex min-w-0 flex-col gap-4">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <p className="min-w-0 text-label text-muted-foreground">Sources sync a provider&apos;s catalogue into {kbName}. Documents they find are ingested like uploads.</p>
        <Button variant="primary" size="sm" onClick={() => setAdding(true)}>Add source</Button>
      </div>
      {workflowKnown && !runReady ? (
        <div data-testid="sync-workflow-missing" className="flex min-w-0 flex-wrap items-center gap-3 rounded-md border border-border bg-muted p-3">
          <p className="min-w-0 flex-1 text-label text-foreground">Syncs run through the KB Source Synchronization workflow, which is not published for this tenant yet.</p>
          <Button variant="secondary" size="sm" disabled={publishing} onClick={() => void publish()}>{publishing ? "Publishing…" : "Publish the sync workflow"}</Button>
          {publishError ? <p role="alert" className="w-full text-label text-destructive-text">{publishError}</p> : null}
        </div>
      ) : null}
      {started ? (
        <p role="status" data-testid="sync-started" className="text-label text-foreground">
          Sync started for {started.sourceName}.{" "}
          <Link href={routes.workflowRun(started.workflowId, started.runId)} className="font-medium underline underline-offset-4 hover:no-underline" aria-label="View workflow run">View run</Link>
        </p>
      ) : null}
      {loadError ? (
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <p role="alert" className="text-label text-destructive-text">{loadError}</p>
          <Button variant="secondary" size="sm" onClick={() => void load()}>Retry</Button>
        </div>
      ) : sources === null ? (
        <p className="text-label text-muted-foreground">Loading sources…</p>
      ) : sources.length === 0 ? (
        <p data-testid="sources-empty" className="text-label text-muted-foreground">No sources yet. Add a connection in Settings, then add a source here.</p>
      ) : (
        <ul aria-label="Sources" data-testid="sources-list" className="min-w-0">
          {sources.map((source) => {
            const open = expanded.has(source.id);
            const checkpoint = source.last_checkpoint_summary;
            return (
              <li key={source.id} data-source={source.id} className="flex min-w-0 flex-col gap-2 border-b border-border py-3 last:border-b-0">
                <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                  <span className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
                    <span className="break-all text-label font-medium text-foreground">{source.name}</span>
                    <span className="text-caption text-muted-foreground">{adapterLabel(adapters, source.provider)}</span>
                    <Badge variant={source.enabled ? "success" : "neutral"}>{source.enabled ? "Enabled" : "Disabled"}</Badge>
                  </span>
                  <span className="flex flex-wrap gap-1.5">
                    <Button variant="primary" size="sm" aria-label={`Run sync for ${source.name}`} disabled={!runReady || !source.enabled} onClick={() => setRunning(source)}>Run sync</Button>
                    <Button variant="secondary" size="sm" aria-label={`Show syncs for ${source.name}`} aria-expanded={open} onClick={() => toggleSyncs(source.id)}>Syncs</Button>
                    <Button variant="secondary" size="sm" aria-label={`${source.enabled ? "Disable" : "Enable"} ${source.name}`} onClick={() => void toggle(source)}>{source.enabled ? "Disable" : "Enable"}</Button>
                  </span>
                </div>
                <p className="text-caption text-muted-foreground">
                  {source.last_successful_sync_at ? `Last successful sync ${formatWhen(source.last_successful_sync_at)}` : "Never synced"}
                  {` · ${checkpoint.segments} ${checkpoint.segments === 1 ? "segment" : "segments"} walked`}
                  {checkpoint.has_cursor ? " · cursor stored" : ""}
                </p>
                {open ? <SyncList kbId={kbId} sourceId={source.id} workflowId={workflowId} refreshKey={refresh} /> : null}
              </li>
            );
          })}
        </ul>
      )}
      <ConflictNotice state={conflict} onReload={() => void load()} />
      <p role="status" data-testid="sources-status" className="min-h-5 text-label text-muted-foreground">{status}</p>
      {adding ? <AddSourceSheet kbId={kbId} onClose={() => setAdding(false)} onAdded={() => void load()} /> : null}
      {running ? (
        <RunSyncSheet
          source={running}
          onClose={() => setRunning(null)}
          onStarted={(result) => {
            setStarted({ ...result, sourceName: running.name });
            setExpanded((current) => new Set(current).add(running.id));
            setRunning(null);
            setRefresh((n) => n + 1);
            void load();
          }}
        />
      ) : null}
    </div>
  );
}
