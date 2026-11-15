import { withQuery } from "./query";

/** The approval Sheet's query parameter on `/approvals` (`?approval=<run_id>:<approval_key>`). */
export const APPROVAL_SHEET_PARAM = "approval";

export interface ApprovalsRouteOptions {
  /** `decided` lists decided approvals; `pending` (the default) is left out of the URL. */
  status?: "pending" | "decided" | null;
  kind?: "tool" | "step" | null;
  /** Filter by agent id (`?agent=`). */
  agent?: string | null;
}

/**
 * The Approvals inbox (AC.15). Every human decision (gated agent tool calls and workflow approval
 * steps) is listed at `/approvals`; a row opens the approval Sheet in place.
 */
export const approvalRoutes = {
  /** `/approvals?status=&kind=&agent=` (keys in that order; defaults left out). */
  approvals: (opts: ApprovalsRouteOptions = {}): string =>
    withQuery("/approvals", {
      status: opts.status === "decided" ? "decided" : undefined,
      kind: opts.kind,
      agent: opts.agent,
    }),
  /** `/approvals?approval=<run_id>:<approval_key>`: the inbox with one approval's Sheet open. */
  approval: (runId: string, approvalKey: string, opts: ApprovalsRouteOptions = {}): string => {
    const base = approvalRoutes.approvals(opts);
    const params = new URLSearchParams(base.includes("?") ? base.slice(base.indexOf("?") + 1) : "");
    params.set(APPROVAL_SHEET_PARAM, `${runId}:${approvalKey}`);
    return `/approvals?${params.toString()}`;
  },
};
