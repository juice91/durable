"use client";
import { AGENT_PERMISSIONS } from "./agentPermissions";
import { usePermissions } from "@/lib/permissions";
import React, { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { MessageSquare } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { Agent, AgentVersion, AgentSpec } from "@/lib/api";
import { routes } from "@/lib/routes";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button } from "@/components/ui/Button";
import { AgentEditor } from "@/components/agents/AgentEditor";
import { publishWarningsOf, type PublishWarning } from "@/components/agents/agentCapabilities";
import { PublishWarnings } from "@/components/agents/PublishWarnings";
import { AgentVersionsPanel, useVersionRestore } from "@/components/agents/AgentVersionsPanel";
import { canonicalJson, errorTarget } from "@/components/agents/specSections";
import { stepForError } from "@/components/agents/stepper/registry";
import { readableError } from "@/components/agents/readableError";
import { CompareSheet } from "@/components/agents/stepper/CompareSheet";
import { useRestoreFocus } from "./restoreFocus";
import { useEditorLayout } from "@/components/agents/stepper/useEditorLayout";
import { useEditorQuery } from "@/components/agents/stepper/useEditorQuery";
import { useUnsavedChangesGuard } from "@/components/agents/stepper/useUnsavedChangesGuard";
import { navigateGuarded } from "@/components/frame/navigationGuard";
import { useRouter } from "next/navigation";
import { useAgentPage } from "./AgentPageContext";
import { SuggestedRules } from "@/components/evals/SuggestedRules";
import { AttachedScorers } from "./AttachedScorers";
import { ConflictNotice } from "@/components/shared/ConflictNotice";
import { useConflict } from "@/lib/useConflict";

const notice = "rounded-md border p-3 text-label";

/** The saved draft differs from the latest published version (the versions list's Draft row). */
export function draftDiffersFromLatest(agent: Agent, versions: AgentVersion[]): boolean {
  const latest = versions.reduce<AgentVersion | null>((best, v) => (best === null || v.version > best.version ? v : best), null);
  if (!latest?.spec) return false;
  return canonicalJson(agent.draft_spec) !== canonicalJson(latest.spec);
}

/**
 * The agent page's Editor tab (SL.11): the stepped editor (`?step=`), the versions column with row
 * menus, and the compare Sheet (`?compare=a..b`) with restore inside. Header actions: Save draft,
 * Compare with latest, Test draft (Chat with the draft) and Publish. Unsaved edits are guarded on
 * navigation. It loads the agent itself and refreshes the agent page's header and Config panel after
 * a save, publish or restore.
 */
export function EditorTab({ agentId }: { agentId: string }) {
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  // The 422 locations of `saveError` (pydantic `detail[].loc`), for routing it to a step.
  const [saveErrorLocations, setSaveErrorLocations] = useState<(string | number)[][]>([]);
  const [versions, setVersions] = useState<AgentVersion[]>([]);
  const [scorersKey, setScorersKey] = useState(0); // bumped when a suggested rule was attached: reloads the list
  const [submitting, setSubmitting] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [lastPublishResult, setLastPublishResult] = useState<{ type: "success"; version: number; warnings?: PublishWarning[] } | { type: "conflict"; message: string } | null>(null);
  const [isDirty, setIsDirty] = useState(false);
  // Bumped after a restore replaces the draft, so the editor re-reads it.
  const [editorKey, setEditorKey] = useState(0);
  const page = useAgentPage();
  const refreshPage = page?.reload;
  const query = useEditorQuery();
  const layout = useEditorLayout();
  const guardDialog = useUnsavedChangesGuard(isDirty);
  const router = useRouter();
  const permissions = usePermissions("agent", agentId, AGENT_PERMISSIONS);
  const mayEdit = permissions.can("edit");
  const mayPublish = permissions.can("publish");
  // A save refused with 412 (someone else changed the draft) is held as a notice, not an error.
  const conflict = useConflict<Agent>({ onSaved: (saved) => applySaved(saved as Agent) });

  function applySaved(updated: Agent) {
    // The editor re-reports dirty against the saved spec (edits typed during the save stay dirty).
    setAgent(updated);
    setSaveError(null);
    void refreshPage?.();
  }

  /** 重新載入: the current draft replaces the editor's content, local edits are discarded. */
  const reloadCurrent = (current: Agent | undefined) => {
    if (current) {
      setAgent(current);
      setEditorKey((key) => key + 1);
      setIsDirty(false);
      setSaveError(null);
      void refreshPage?.();
    } else {
      void api.agents.get(agentId).then((fresh) => reloadCurrent(fresh));
    }
  };

  useEffect(() => {
    const loadAgent = async () => {
      try {
        if (!agentId) return;
        const data = await api.agents.get(agentId);
        setAgent(data);
        const versionsData = await api.versions.list(agentId);
        setVersions(versionsData);
        setLoadError(null);
        setSaveError(null);
        setIsDirty(false);
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 404) {
          setLoadError("Agent not found");
        } else {
          setLoadError("Failed to load agent");
        }
      }
    };
    loadAgent();
  }, [agentId]);

  const handleSaveDraft = async (spec: AgentSpec) => {
    if (!agent || submitting) return;
    setSubmitting(true);
    setSaveError(null);
    setSaveErrorLocations([]);
    try {
      const updated = await conflict.guard(() => api.agents.update(agent.id, spec));
      if (updated !== undefined) applySaved(updated);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setSaveErrorLocations((err.details ?? []).map((detail) => detail.loc));
        setSaveError(err.message || "Failed to save draft");
      } else {
        setSaveError("Failed to save draft");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handlePublish = async () => {
    if (!agent || publishing) return;
    setPublishing(true);
    setLastPublishResult(null);
    setSaveError(null);
    setSaveErrorLocations([]);
    try {
      const version = await api.versions.create(agent.id);
      // AC.13: the publish response's `warnings` (AC.7: an unpriced model under a cost cap...).
      setLastPublishResult({ type: "success", version: version.version, warnings: publishWarningsOf(version) });
      const versionsData = await api.versions.list(agent.id);
      setVersions(versionsData);
      void refreshPage?.();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409) {
        setLastPublishResult({ type: "conflict", message: err.message });
      } else if (err instanceof ApiError) {
        setSaveErrorLocations((err.details ?? []).map((detail) => detail.loc));
        setSaveError(err.message || "Failed to publish version");
      } else {
        setSaveError("Failed to publish version");
      }
    } finally {
      setPublishing(false);
    }
  };

  const handleRestored = async () => {
    if (!agent) return;
    setLastPublishResult(null);
    try {
      const [updated, versionsData] = await Promise.all([api.agents.get(agent.id), api.versions.list(agent.id)]);
      setAgent(updated);
      setVersions(versionsData);
      // The remounted editor reports its own (clean) state.
      setEditorKey((key) => key + 1);
      void refreshPage?.();
    } catch {
      setSaveError("Version restored, but reloading the agent failed; refresh the page");
    }
  };

  const latestVersion = versions.reduce((max, v) => Math.max(max, v.version), 0);
  const draftChanged = useMemo(() => (agent ? draftDiffersFromLatest(agent, versions) : false), [agent, versions]);
  // Restore from the compare Sheet: the same flow as a row's "Restore this version".
  // After a restore from the compare Sheet, focus goes to the "Version N published" notice: the
  // Restore button, the confirm and the Sheet all go away (restoreFocus.ts).
  const restoreFocus = useRestoreFocus();
  const compareRestore = useVersionRestore({
    agentId: agent?.id,
    latest: latestVersion,
    hasUnsavedEdits: isDirty,
    onRestored: () => {
      restoreFocus.arm();
      query.closeCompare();
      void handleRestored().finally(() => setNoticeFocusTick((tick) => tick + 1));
    },
  });
  const [noticeFocusTick, setNoticeFocusTick] = useState(0);
  const compareOpen = query.compare !== null;
  const { settle } = restoreFocus;
  useEffect(() => {
    settle(compareOpen);
  }, [settle, noticeFocusTick, compareOpen, editorKey, compareRestore.notice]);

  if (loadError && !agent) {
    return (
      <div className="flex items-center justify-center py-12">
        <p className="text-destructive-text">{loadError}</p>
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="flex items-center justify-center py-12">
        <p className="text-muted-foreground">Loading...</p>
      </div>
    );
  }

  const busy = publishing || submitting;
  const actions = (
    <>
      {latestVersion > 0 ? (
        <Button size="sm" variant="ghost" onClick={() => query.openCompare(latestVersion, "draft")} disabled={isDirty}>
          {`Compare with latest (v${latestVersion})`}
        </Button>
      ) : null}
      {permissions.can("run") ? (
        <Button size="sm" variant="ghost" asChild>
          <Link href={routes.agentChat(agent.id, { version: "draft" })} data-testid="editor-test-draft">
            <MessageSquare strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            Test draft
          </Link>
        </Button>
      ) : null}
      {mayPublish ? (
        <Button size="sm" variant="primary" onClick={handlePublish} disabled={busy || isDirty}>
          {busy ? "Processing..." : "Publish"}
        </Button>
      ) : null}
    </>
  );

  const notices = (
    <>
      <p className="text-caption text-muted-foreground">
        ID: <span className="font-mono">{agent.id}</span>
      </p>
      {/* Errors a step owns show in that step instead (inline, or at the top of the step). */}
      <ConflictNotice state={conflict} onReload={reloadCurrent} />
      {saveError && !errorTarget(saveError) && !stepForError(saveError) ? (
        <div role="alert" data-testid="agent-save-error" className={`${notice} border-destructive/40 bg-destructive/10 text-destructive-text`}>
          {readableError(saveError)}
        </div>
      ) : null}
      {lastPublishResult?.type === "success" ? (
        <div className={`${notice} border-success/40 bg-success/10 text-success-text`}>Version {lastPublishResult.version} published</div>
      ) : null}
      {lastPublishResult?.type === "success" ? <PublishWarnings warnings={lastPublishResult.warnings ?? []} /> : null}
      {lastPublishResult?.type === "conflict" ? (
        <div className={`${notice} border-warning/40 bg-warning/10 text-warning-text`}>{lastPublishResult.message}</div>
      ) : null}
      {isDirty ? <div className={`${notice} border-warning/40 bg-warning/10 text-warning-text`}>Save the draft before publishing</div> : null}
      {compareRestore.notice ? (
        <p key="compare-restore-notice" role="status" data-restore-notice="" tabIndex={-1} className={`${notice} border-border bg-muted text-foreground outline-none`}>
          {compareRestore.notice}
        </p>
      ) : null}
      {compareRestore.notice ? <PublishWarnings warnings={compareRestore.warnings} /> : null}
    </>
  );

  return (
    <div data-testid="agent-editor-tab" className="w-full min-w-0">
      <AgentEditor
        key={editorKey}
        readOnly={!mayEdit}
        initialData={agent.draft_spec}
        onSubmit={handleSaveDraft}
        submitLabel="Save draft"
        onDirtyChange={setIsDirty}
        serverError={saveError}
        serverErrorLocations={saveErrorLocations}
        stepper={{
          activeStep: query.step,
          onStepChange: query.setStep,
          layout,
          actions,
          notices,
          versions:
            versions.length === 0 ? (
              <div data-testid="agent-versions-empty">
                <h2 className="mb-2 text-title font-semibold text-foreground">Versions</h2>
                <p className="text-label text-muted-foreground">No published versions yet. Publish the draft to create v1.</p>
              </div>
            ) : (
            <AgentVersionsPanel
              versions={versions}
              agentId={agent.id}
              hasUnsavedEdits={isDirty}
              onRestored={handleRestored}
              mayRestore={mayPublish}
              onCompare={query.openCompare}
              draftChanged={draftChanged}
              onNavigate={(href) => navigateGuarded(href, (target) => router.push(target))}
            />
          ),
        }}
      />
      <section aria-labelledby="editor-scorers-title" className="mt-6 border-t border-border pt-4">
        <h2 id="editor-scorers-title" className="mb-2 text-title font-semibold text-foreground">Scorers</h2>
        <AttachedScorers key={scorersKey} agentId={agent.id} mayEdit={mayEdit} />
        {mayEdit ? (
          <div className="mt-4">
            <SuggestedRules target={{ kind: "agent", id: agent.id, name: agent.name }} heading="Suggested scorers" onChanged={() => setScorersKey((k) => k + 1)} />
          </div>
        ) : null}
      </section>
      <CompareSheet
        agentId={agent.id}
        compare={query.compare}
        latest={latestVersion}
        onClose={query.closeCompare}
        restore={compareRestore}
        mayRestore={mayPublish}
        onCloseAutoFocus={restoreFocus.onSheetCloseAutoFocus}
      />
      {compareRestore.dialog}
      {guardDialog}
    </div>
  );
}
