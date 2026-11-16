"use client";

import React, { useEffect, useState } from "react";
import { ConnectAgUiAction } from "@/components/apiKeys/ConnectAgUiSheet";
import { PageHeader } from "@/components/frame/PageHeader";
import type { LinkTab } from "@/components/ui/Tabs";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import type { WorkflowTab } from "@/lib/routes/workflows";
import { listSchedules } from "@/lib/schedules";
import { listWorkflows } from "@/lib/workflows";

/** Graph, Runs, Traces, and the Schedules tab from the workflow tour. */
export type WorkflowHeaderTab = WorkflowTab | "schedules";

/** The workflow page's tabs: Graph, Runs, Traces, and Schedules. */
export function workflowTabs(workflowId: string, active: WorkflowHeaderTab, scheduleCount = 0): LinkTab[] {
  return [
    { href: routes.workflow(workflowId), label: "Graph", active: active === "graph", testId: "workflow-tab-graph" },
    { href: routes.workflow(workflowId, { tab: "runs" }), label: "Runs", active: active === "runs", testId: "workflow-tab-runs" },
    { href: routes.workflowTraces(workflowId), label: "Traces", active: active === "traces", testId: "workflow-tab-traces" },
    { href: routes.workflowSchedule(workflowId), label: "Schedules", count: scheduleCount > 0 ? scheduleCount : undefined, active: active === "schedules", testId: "workflow-tab-schedules" },
  ];
}

/** Every workflow as a switcher option, loaded once on mount (empty until then, or on failure). */
function useWorkflowOptions(): Array<{ value: string; label: string }> {
  const [options, setOptions] = useState<Array<{ value: string; label: string }>>([]);
  useEffect(() => {
    let cancelled = false;
    void Promise.resolve()
      .then(() => listWorkflows())
      .then((workflows) => {
        if (!cancelled && Array.isArray(workflows)) setOptions(workflows.map((w) => ({ value: w.id, label: w.name })));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  return options;
}

export interface WorkflowHeaderProps {
  workflowId: string;
  /** The workflow's name once loaded. */
  name: string | null;
  tab: WorkflowHeaderTab;
  actions?: React.ReactNode;
  /** The Schedules tab's count when the page already knows it (it then skips its own load). */
  scheduleCount?: number;
}

/** How many schedules the workflow has (0 until loaded, or on failure). */
function useScheduleCount(workflowId: string, known: number | undefined): number {
  const [count, setCount] = useState(0);
  useEffect(() => {
    if (known !== undefined || !workflowId) return;
    const controller = new AbortController();
    void listSchedules({ workflowId, limit: 200, signal: controller.signal })
      .then((page) => {
        if (!controller.signal.aborted) setCount(page.items.length);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [workflowId, known]);
  return known ?? count;
}

/**
 * The workflow pages' header: Workflows › {name} (the h1, with a "Switch workflow" combobox that
 * keeps the current tab), the page actions, Connect (AG-UI), and the Graph · Runs · Traces · Schedules tabs.
 * Needs a `<Suspense>` boundary above it (the Connect Sheet reads the URL).
 */
export function WorkflowHeader({ workflowId, name, tab, actions, scheduleCount }: WorkflowHeaderProps) {
  const options = useWorkflowOptions();
  const schedules = useScheduleCount(workflowId, scheduleCount);
  const hrefFor = (id: string) => {
    if (tab === "traces") return routes.workflowTraces(id);
    if (tab === "schedules") return routes.workflowSchedule(id);
    return routes.workflow(id, { tab: tab === "runs" ? "runs" : null });
  };
  const section = tab === "graph" ? "" : tab === "runs" ? "Runs" : tab === "traces" ? "Traces" : "Schedules";
  return (
    <PageHeader
      crumbs={[
        navCrumb("workflows"),
        {
          label: name ?? "Workflow",
          switcher: options.length > 1 ? { label: "Switch workflow", options, value: workflowId, hrefFor, testId: "workflow-switcher" } : undefined,
        },
      ]}
      documentTitle={name ? `${name}${section ? ` · ${section}` : ""}` : "Workflow"}
      actions={
        <>
          {actions}
          <ConnectAgUiAction target={{ kind: "workflow", id: workflowId }} name={name} />
        </>
      }
      tabs={workflowTabs(workflowId, tab, schedules)}
      tabsLabel="Workflow sections"
    />
  );
}
