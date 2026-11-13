import axe, { type AxeResults, type RunOptions } from "axe-core";
import { expect } from "vitest";

/**
 * Rules jsdom cannot evaluate: it does no layout or painting, so colour contrast is checked by
 * `test/contrast.test.ts` from the tokens instead; and a single rendered component is not a page
 * (no <main>, no <h1>), so the page-level landmark rules are left to the frame's own checks.
 */
const JSDOM_DISABLED_RULES = ["color-contrast", "region", "landmark-one-main", "page-has-heading-one"];

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
  return axe.run(container, { ...options, rules });
}

/**
 * Fails the test with a readable list when axe finds any violation in `container`. Portaled content
 * (dialogs, sheets, popovers) renders into <body>, so pass `document.body` (the default is the whole
 * document) when checking an open overlay.
 */
export async function expectNoAxeViolations(container: Element | Document = document, options: RunOptions = {}): Promise<void> {
  const results = await runAxe(container, options);
  expect(results.violations, describeViolations(results)).toEqual([]);
}
