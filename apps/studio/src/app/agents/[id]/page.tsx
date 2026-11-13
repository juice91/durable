"use client";
import React, { useState, useEffect } from "react";
import { useRouter, useParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { Agent, AgentVersion, AgentSpec } from "@/lib/api";
import { AgentEditor } from "@/components/agents/AgentEditor";
import { AgentVersionsPanel } from "@/components/agents/AgentVersionsPanel";
import { VersionCompare } from "@/components/agents/VersionCompare";
import { errorTarget } from "@/components/agents/specSections";
import { routes } from "@/lib/routes";

export default function AgentDetailPage() {
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [versions, setVersions] = useState<AgentVersion[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [lastPublishResult, setLastPublishResult] = useState<{ type: "success"; version: number } | { type: "conflict"; message: string } | null>(null);
  const [isDirty, setIsDirty] = useState(false);
  const [comparingDraft, setComparingDraft] = useState(false);
  // Bumped after a restore replaces the draft, so the editor re-reads it.
  const [editorKey, setEditorKey] = useState(0);
  const router = useRouter();
  const params = useParams();

  useEffect(() => {
    const loadAgent = async () => {
      try {
        if (!params.id) return;
        const data = await api.agents.get(String(params.id));
        setAgent(data);
        const versionsData = await api.versions.list(String(params.id));
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
  }, [params.id]);

  const handleSaveDraft = async (spec: AgentSpec) => {
    if (!agent || submitting) return;
    setSubmitting(true);
    setSaveError(null);
    try {
      const updated = await api.agents.update(agent.id, spec);
      setAgent(updated);
      setSaveError(null);
      setIsDirty(false);
      setComparingDraft(false);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
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
    try {
      const version = await api.versions.create(agent.id);
      setLastPublishResult({ type: "success", version: version.version });
      // Refresh versions
      const versionsData = await api.versions.list(agent.id);
      setVersions(versionsData);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409) {
        setLastPublishResult({ type: "conflict", message: err.message });
      } else if (err instanceof ApiError) {
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
    setComparingDraft(false);
    try {
      const [updated, versionsData] = await Promise.all([api.agents.get(agent.id), api.versions.list(agent.id)]);
      setAgent(updated);
      setVersions(versionsData);
      setIsDirty(false);
      setEditorKey((key) => key + 1);
    } catch {
      setSaveError("Version restored, but reloading the agent failed; refresh the page");
    }
  };

  const latestVersion = versions.reduce((max, v) => Math.max(max, v.version), 0);

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
        <p className="text-card-foreground/60">Loading...</p>
      </div>
    );
  }

  return (
    <div className="max-w-3xl mx-auto">
        <div className="mb-6">
          <button
            onClick={() => router.push(routes.agents())}
            className="text-sm text-card-foreground/80 hover:text-card-foreground mb-4 inline-flex items-center"
          >
            ← Back to agents
          </button>
          <h1 className="text-2xl font-bold text-card-foreground">Edit Agent</h1>
          <p className="text-sm text-muted-foreground mt-1">ID: {agent.id}</p>
        </div>

        {/* Errors that name a part of the form (a processor, the instructions) show inline there instead. */}
        {saveError && !errorTarget(saveError) && (
          <div role="alert" data-testid="agent-save-error" className="mb-4 p-3 border rounded-md text-sm border-destructive/40 bg-destructive/10 text-destructive-text">
            {saveError}
          </div>
        )}

        <div className="mb-8">
          <h2 className="text-lg font-semibold mb-3 text-card-foreground">Edit Agent</h2>
          {lastPublishResult?.type === "success" && (
            <div className="mb-3 p-3 border rounded-md text-sm border-success/40 bg-success/10 text-success-text">
              Version {lastPublishResult.version} published
            </div>
          )}
          {lastPublishResult?.type === "conflict" && (
            <div className="mb-3 p-3 border rounded-md text-sm border-warning/40 bg-warning/10 text-warning-text">
              {lastPublishResult.message}
            </div>
          )}
          <AgentEditor
            key={editorKey}
            initialData={agent.draft_spec}
            onSubmit={handleSaveDraft}
            submitLabel="Save draft"
            onDirtyChange={setIsDirty}
            serverError={saveError}
          />
          <div className="mt-4">
            {isDirty ? (
              <div className="mb-3 p-3 border rounded-md text-sm border-warning/40 bg-warning/10 text-warning-text">
                Save the draft before publishing
              </div>
            ) : null}
            {latestVersion > 0 ? (
              <div className="mb-3">
                <button
                  type="button"
                  onClick={() => setComparingDraft((open) => !open)}
                  disabled={isDirty}
                  aria-expanded={comparingDraft}
                  className="w-full rounded-md border border-border px-4 py-2 text-sm text-foreground hover:bg-accent focus:outline-none focus:ring-2 focus:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {comparingDraft ? "Hide compare" : `Compare with latest (v${latestVersion})`}
                </button>
                {comparingDraft && !isDirty ? (
                  <div className="mt-3">
                    <VersionCompare agentId={agent.id} base={latestVersion} target="draft" onClose={() => setComparingDraft(false)} />
                  </div>
                ) : null}
              </div>
            ) : null}
            <button
              onClick={handlePublish}
              disabled={publishing || submitting || isDirty}
              className="w-full px-4 py-2 bg-primary text-primary-foreground rounded-md hover:bg-primary/90 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {publishing || submitting ? "Processing..." : "Publish"}
            </button>
          </div>
        </div>

        <AgentVersionsPanel versions={versions} agentId={agent.id} hasUnsavedEdits={isDirty} onRestored={handleRestored} />
    </div>
  );
}
