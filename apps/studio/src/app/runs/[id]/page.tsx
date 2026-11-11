"use client";

import React, { useState, useEffect } from "react";
import { Layout } from "@/components/layout/Layout";
import { useRouter, useParams, usePathname } from "next/navigation";
import { getRun, listEvents, subscribeRun, type Run, type RunEvent, deltaReducer } from "@/lib/runs";
import { StatusPill } from "@/components/runs/StatusPill";
import { EventTimeline } from "@/components/runs/EventTimeline";

export default function RunDetailPage() {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [partialText, setPartialText] = useState<Record<string, { text: string; attempt: string }>>({});
  const router = useRouter();
  const params = useParams();

  const runId = typeof params.id === "string" ? params.id : undefined;

  useEffect(() => {
    if (!runId) return;

    const loadRun = async () => {
      try {
        const runData = await getRun(runId);
        setRun(runData);

        const eventsData = await listEvents(runId);
        setEvents(eventsData);
        setLoadError(null);
      } catch (err: unknown) {
        if (err instanceof Error && (err as any).status === 404) {
          setLoadError("Run not found");
        } else {
          setLoadError("Failed to load run");
        }
      }
    };

    loadRun();
  }, [runId]);

  // Subscribe once the run has loaded; status frames update `run`, so depend only on its presence
  // (depending on `run` itself would tear the stream down and replay it on every status frame).
  const runLoaded = run !== null;
  useEffect(() => {
    if (!runId || !runLoaded) return;

    const unsubscribe = subscribeRun(runId, {
      onEvent: (event) => {
        setEvents((prev) => {
          if (prev.some((e) => e.seq === event.seq)) {
            return prev;
          }
          return [...prev, event].sort((a, b) => a.seq - b.seq);
        });
        // Durability rule 4: the durable model message replaces its partial text.
        if (event.type === "model_message") {
          setPartialText((prev) => deltaReducer(prev, { type: "model_message", payload: event }));
        } else if (event.type === "run_completed" || event.type === "run_failed") {
          setPartialText((prev) => deltaReducer(prev, { type: event.type === "run_completed" ? "done" : "failed" }));
        }
      },
      onDelta: (delta) => {
        setPartialText((prev) => deltaReducer(prev, { type: "delta", payload: delta }));
      },
      onStatus: (status) => {
        setRun((prev) => (prev ? { ...prev, display_status: status.display_status } : null));
      },
    });

    return unsubscribe;
  }, [runId, runLoaded]);

  const getStatus = () => {
    if (!run) return "queued";
    // Check for recovery status based on heartbeat
    if (run.display_status === "running" && run.heartbeat_at) {
      const heartbeatAge = Date.now() - new Date(run.heartbeat_at).getTime();
      if (heartbeatAge > 20000) { // 20 seconds
        return "recovering";
      }
    }
    return run.display_status;
  };

  const status = getStatus();

  if (loadError && !run) {
    return (
      <Layout>
        <div className="flex items-center justify-center py-12">
          <p className="text-red-500">{loadError}</p>
        </div>
      </Layout>
    );
  }

  if (!run) {
    return (
      <Layout>
        <div className="flex items-center justify-center py-12">
          <p className="text-gray-500">Loading run...</p>
        </div>
      </Layout>
    );
  }

  return (
    <Layout>
      <div className="max-w-4xl mx-auto">
        <div className="mb-6">
          <button
            onClick={() => router.push("/agents")}
            className="text-sm text-gray-600 hover:text-gray-900 mb-4 inline-flex items-center"
          >
            ← Back to agents
          </button>
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold">Run {run.id}</h1>
              <p className="text-sm text-gray-500 mt-1">
                Agent: {run.agent_id} | Version: {run.version ?? "latest"}
              </p>
            </div>
            <StatusPill status={status} showRecoveringNotice={status === "recovering"} />
          </div>

          {status === "recovering" && (
            <div className="mt-4 p-3 bg-yellow-50 border border-yellow-200 rounded-md text-yellow-700 text-sm">
              ⚠️ The worker is restarting. Events may be delayed as the system recovers.
            </div>
          )}
        </div>

        <div className="bg-white rounded-lg shadow p-6">
          <h2 className="text-lg font-semibold mb-4">Event Timeline</h2>
          <EventTimeline events={events} currentPartialText={partialText} />
        </div>
      </div>
    </Layout>
  );
}
