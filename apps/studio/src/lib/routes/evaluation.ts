export const evaluationRoutes = {
  /** `/datasets`: the test suites of every agent (W1.5). A row opens the suite in the agent's Tests tab. */
  datasets: (): string => "/datasets",
  /** `/scorers`: the evaluators the suites use, with their recent scores. */
  scorers: (): string => "/scorers",
  /** `/experiments`: the eval runs of every agent, newest first. A row opens the run in the agent's Tests tab. */
  experiments: (): string => "/experiments",
};
