import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

// Pre-merge review N2: React Flow's stock credit is #999 on a half-white patch. globals.css restyles
// it on our tokens (muted-foreground is checked >= 4.5:1 on every surface by contrast.test.ts).
const css = fs.readFileSync(path.join(__dirname, "..", "app", "globals.css"), "utf8");

function rule(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, selector).toBeGreaterThan(-1);
  return css.slice(start, css.indexOf("}", start));
}

describe("React Flow attribution on tokens", () => {
  it("has no background patch and a muted-foreground link, more specific than React Flow's own rules", () => {
    expect(rule(".react-flow .react-flow__attribution")).toMatch(/background:\s*transparent/);
    const link = rule(".react-flow .react-flow__attribution a");
    expect(link).toMatch(/color:\s*var\(--color-muted-foreground\)/);
    expect(link).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(rule(".react-flow .react-flow__attribution a:focus-visible")).toMatch(/outline:.*var\(--color-ring\)/);
  });

  it("the graph no longer paints React Flow's attribution background", () => {
    const graph = fs.readFileSync(path.join(__dirname, "..", "components", "workflows", "WorkflowGraph.tsx"), "utf8");
    expect(graph).toMatch(/"--xy-attribution-background-color": "transparent"/);
    expect(graph).not.toMatch(/hideAttribution/);
  });
});
