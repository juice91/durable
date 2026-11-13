import { seg } from "./query";

export const workflowRoutes = {
  /** `/workflows`: the workflows list. */
  workflows: (): string => "/workflows",
  /** `/workflows/:id`: the workflow detail page. */
  workflow: (id: string): string => `/workflows/${seg(id)}`,
  /** `/workflows/:id/runs/:runId`: one workflow run. */
  workflowRun: (id: string, runId: string): string => `/workflows/${seg(id)}/runs/${seg(runId)}`,
};
