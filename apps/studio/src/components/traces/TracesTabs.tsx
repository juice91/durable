"use client";

import React from "react";
import { LinkTabs } from "@/components/ui/Tabs";
import { routes } from "@/lib/routes";

export type TracesTab = "traces" | "spans";

/** The Traces page's tab strip: Traces, Spans and Threads (its own page, linked from here). */
export function TracesTabs({ active }: { active: TracesTab }) {
  return (
    <LinkTabs
      label="Traces sections"
      className="px-4"
      items={[
        { href: routes.traces(), label: "Traces", active: active === "traces" },
        { href: routes.spans(), label: "Spans", active: active === "spans" },
        { href: routes.threads(), label: "Threads", active: false },
      ]}
    />
  );
}
