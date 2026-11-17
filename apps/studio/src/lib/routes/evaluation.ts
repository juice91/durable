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
  /** `/workflows/:id/tests?suite=&run=`: a workflow's test suites, and one eval run of a suite. */
  workflowTests: (id: string, opts: { suite?: string | null; run?: string | null } = {}): string =>
    withQuery(`/workflows/${seg(id)}/tests`, { suite: opts.suite, run: opts.run }),
};
