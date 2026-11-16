import { seg, withQuery } from "./query";

export const evaluationRoutes = {
  /** `/datasets`: the test suites of every agent and workflow (W1.5). A row opens `/datasets/:id`. */
  datasets: (): string => "/datasets",
  /** `/datasets/:id`: one dataset (suite) with its cases, imports and schema. */
  dataset: (id: string): string => `/datasets/${seg(id)}`,
  /** `/scorers`: the evaluators the suites use, with their recent scores. */
  scorers: (): string => "/scorers",
  /** `/experiments`: the eval runs of every agent and workflow, newest first. */
  experiments: (): string => "/experiments",
  /** `/workflows/:id/tests?suite=`: a workflow's test suites. */
  workflowTests: (id: string, opts: { suite?: string | null } = {}): string =>
    withQuery(`/workflows/${seg(id)}/tests`, { suite: opts.suite }),
};
