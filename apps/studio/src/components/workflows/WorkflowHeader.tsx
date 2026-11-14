"use client";

import React, { useEffect, useState } from "react";
import { PageHeader } from "@/components/frame/PageHeader";
import type { LinkTab } from "@/components/ui/Tabs";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import type { WorkflowTab } from "@/lib/routes/workflows";
import { listWorkflows } from "@/lib/workflows";

/** The workflow page's tabs: Graph (`/workflows/:id`), Runs (`?tab=runs`) and Traces (`/workflows/:id/traces`). */
export function workflowTabs(workflowId: string, active: WorkflowTab): LinkTab[] {
  return [
    { href: routes.workflow(workflowId), label: "Graph", active: active === "graph", testId: "workflow-tab-graph" },
    { href: routes.workflow(workflowId, { tab: "runs" }), label: "Runs", active: active === "runs", testId: "workflow-tab-runs" },
    { href: routes.workflowTraces(workflowId), label: "Traces", active: active === "traces", testId: "workflow-tab-traces" },
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
  tab: WorkflowTab;
  actions?: React.ReactNode;
}

/**
 * The workflow pages' header: Workflows › {name} (the h1, with a "Switch workflow" combobox that
 * keeps the current tab), the page actions and the Graph · Runs · Traces tabs.
 */
export function WorkflowHeader({ workflowId, name, tab, actions }: WorkflowHeaderProps) {
  const options = useWorkflowOptions();
  const hrefFor = (id: string) => (tab === "traces" ? routes.workflowTraces(id) : routes.workflow(id, { tab: tab === "runs" ? "runs" : null }));
  return (
    <PageHeader
      crumbs={[
        navCrumb("workflows"),
        {
          label: name ?? "Workflow",
          switcher: options.length > 1 ? { label: "Switch workflow", options, value: workflowId, hrefFor, testId: "workflow-switcher" } : undefined,
        },
      ]}
      documentTitle={name ? `${name}${tab === "graph" ? "" : ` · ${tab === "runs" ? "Runs" : "Traces"}`}` : "Workflow"}
      actions={actions}
      tabs={workflowTabs(workflowId, tab)}
      tabsLabel="Workflow sections"
    />
  );
}
