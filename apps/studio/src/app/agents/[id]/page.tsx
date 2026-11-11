"use client";
import React, { useState, useEffect } from "react";
import { useRouter, useParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { Agent, AgentVersion, AgentSpec } from "@/lib/api";
import { AgentEditor } from "@/components/agents/AgentEditor";
import { AgentVersionsPanel } from "@/components/agents/AgentVersionsPanel";

export default function AgentDetailPage() {
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [versions, setVersions] = useState<AgentVersion[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [lastPublishResult, setLastPublishResult] = useState<{ type: "success"; version: number } | { type: "conflict"; message: string } | null>(null);
  const [isDirty, setIsDirty] = useState(false);
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

  if (loadError && !agent) {
    return (
      <div className="flex items-center justify-center py-12">
        <p className="text-red-500">{loadError}</p>
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="flex items-center justify-center py-12">
        <p className="text-gray-500">Loading...</p>
      </div>
    );
  }

  return (
    <div className="max-w-3xl mx-auto">
        <div className="mb-6">
          <button
            onClick={() => router.push("/agents")}
            className="text-sm text-gray-600 hover:text-gray-900 mb-4 inline-flex items-center"
          >
            ← Back to agents
          </button>
          <h1 className="text-2xl font-bold">Edit Agent</h1>
          <p className="text-sm text-gray-500 mt-1">ID: {agent.id}</p>
        </div>

        {saveError && (
          <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-md text-red-700 text-sm">
            {saveError}
          </div>
        )}

        <div className="mb-8">
          <h2 className="text-lg font-semibold mb-3">Edit Agent</h2>
          {lastPublishResult?.type === "success" && (
            <div className="mb-3 p-3 bg-green-50 border border-green-200 rounded-md text-green-700 text-sm">
              Version {lastPublishResult.version} published
            </div>
          )}
          {lastPublishResult?.type === "conflict" && (
            <div className="mb-3 p-3 bg-yellow-50 border border-yellow-200 rounded-md text-yellow-700 text-sm">
              {lastPublishResult.message}
            </div>
          )}
          <AgentEditor
            initialData={agent.draft_spec}
            onSubmit={handleSaveDraft}
            submitLabel="Save draft"
            onDirtyChange={setIsDirty}
          />
          <div className="mt-4">
            {isDirty ? (
              <div className="mb-3 p-3 bg-yellow-50 border border-yellow-200 rounded-md text-yellow-700 text-sm">
                Save the draft before publishing
              </div>
            ) : null}
            <button
              onClick={handlePublish}
              disabled={publishing || submitting || isDirty}
              className="w-full px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 focus:outline-none focus:ring-2 focus:ring-green-500 focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {publishing || submitting ? "Processing..." : "Publish"}
            </button>
          </div>
        </div>

        <AgentVersionsPanel versions={versions} />
    </div>
  );
}
