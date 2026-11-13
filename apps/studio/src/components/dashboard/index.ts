// Overview Dashboard (P0.18) pieces; the shared metrics UI lives in @/components/metrics.
import type { ChartId } from "@/components/metrics";

export { DashboardKpis, dashboardKpiCards, DASHBOARD_KPIS, type DashboardKpisProps } from "./DashboardKpis";
export { RecentFailures, runLabel, type RecentFailuresProps } from "./RecentFailures";

/** The trends the dashboard shows (the Metrics page shows these and more). */
export const DASHBOARD_CHARTS: readonly ChartId[] = ["runs", "cost"];
/** Rows of the top agents table and of the recent failures list. */
export const TOP_AGENTS_LIMIT = 5;
export const RECENT_FAILURES_LIMIT = 5;
