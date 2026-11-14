import axe, { type AxeResults, type RunOptions } from "axe-core";
import { expect } from "vitest";

/**
 * Rules jsdom cannot evaluate: it does no layout or painting, so colour contrast is checked by
 * `test/contrast.test.ts` from the tokens instead; and a single rendered component is not a page
 * (no <main>, no <h1>), so the page-level landmark rules are left to the frame's own checks.
 */
const JSDOM_DISABLED_RULES = ["color-contrast", "region", "landmark-one-main", "page-has-heading-one"];

/**
 * Per-test timeout for any test that runs axe. A full axe pass over a page render takes 1–3 s in
 * jsdom, and several in one test exceeded vitest's 5 s default under machine load. Pass it as the
 * third argument of `it(...)` (`it("…", async () => {…}, AXE_TIMEOUT_MS)`); `test/a11y.guard.test.ts`
 * fails when a test calls axe without it.
 */
export const AXE_TIMEOUT_MS = 30_000;

/**
 * Speed only, never what is checked: every rule still runs, but axe skips building the detail of
 * passed, inapplicable and incomplete results, which nothing here reads.
 */
const FAST: RunOptions = { resultTypes: ["violations"] };

function describeViolations(results: AxeResults): string {
  return results.violations
    .map((violation) => {
      const nodes = violation.nodes.map((node) => `    ${node.target.join(" ")}: ${node.failureSummary ?? ""}`).join("\n");
      return `${violation.id} (${violation.impact ?? "n/a"}): ${violation.help}\n${nodes}`;
    })
    .join("\n\n");
}

/** Runs axe-core on `container` (default: the whole document) and returns the results. */
export async function runAxe(container: Element | Document = document, options: RunOptions = {}): Promise<AxeResults> {
  const rules: NonNullable<RunOptions["rules"]> = { ...options.rules };
  for (const id of JSDOM_DISABLED_RULES) rules[id] = rules[id] ?? { enabled: false };
  return axe.run(container, { ...FAST, ...options, rules });
}

/**
 * Fails the test with a readable list when axe finds any violation in `container`. Portaled content
 * (dialogs, sheets, popovers) renders into <body>, so pass `document.body` (the default is the whole
 * document) when checking an open overlay. Give the calling test `AXE_TIMEOUT_MS`.
 */
export async function expectNoAxeViolations(container: Element | Document = document, options: RunOptions = {}): Promise<void> {
  const results = await runAxe(container, options);
  expect(results.violations, describeViolations(results)).toEqual([]);
}

/**
 * Page-level rules on, for full page renders in the frame: one `<main>` landmark, one level-one
 * heading, and all content inside a landmark (`region`).
 */
export const PAGE_RULES: NonNullable<RunOptions["rules"]> = {
  "landmark-one-main": { enabled: true },
  "page-has-heading-one": { enabled: true },
  region: { enabled: true },
};
