/**
 * SL.15: the Studio README (apps/studio/README.md) stays in step with the code it documents: every
 * key Settings lists under "Data in this browser" (BROWSER_DATA), every retired URL pattern
 * (LEGACY_RULES) and every `g` shortcut from the nav registry.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { BROWSER_DATA } from "@/components/settings/browserData";
import { LEGACY_RULES } from "@/lib/routes";
import { navItems } from "@/lib/nav";

const readme = fs.readFileSync(path.resolve(__dirname, "..", "..", "README.md"), "utf8");
const section = (title: string) => {
  const start = readme.indexOf(title);
  if (start < 0) throw new Error(`README has no "${title}" section`);
  const next = readme.indexOf("\n### ", start + title.length);
  return readme.slice(start, next < 0 ? undefined : next);
};

describe("apps/studio/README.md", () => {
  it("lists every BROWSER_DATA key and prefix under 'What is stored in this browser'", () => {
    const storage = section("### What is stored in this browser");
    for (const entry of BROWSER_DATA) {
      for (const key of [...entry.keys, ...entry.prefixes]) {
        // Prefixes are written with what follows them (`studio.agent-layout.v1.<agentId>`).
        expect(storage, `${entry.id}: ${key}`).toContain(`\`${key.replace(/[.:]$/, "")}`);
      }
    }
  });

  it("lists every retired URL pattern under 'Retired URLs'", () => {
    const retired = section("#### Retired URLs");
    for (const rule of LEGACY_RULES) {
      const shown = rule.pattern === "/" ? "`/`" : `\`${rule.pattern}`;
      expect(retired, `${rule.task} ${rule.pattern}`).toContain(shown);
    }
  });

  it("lists every g shortcut under 'Keyboard shortcuts'", () => {
    const keys = section("### Keyboard shortcuts");
    for (const item of navItems.filter((i) => i.gKey)) {
      expect(keys, `${item.id}: g ${item.gKey}`).toContain(`\`g\` then \`${item.gKey}\` | Go to ${item.label}`);
    }
  });
});

describe("THIRD_PARTY_NOTICES.md", () => {
  const studio = path.resolve(__dirname, "..", "..");
  const notices = fs.readFileSync(path.resolve(studio, "..", "..", "THIRD_PARTY_NOTICES.md"), "utf8");
  const pkg = JSON.parse(fs.readFileSync(path.join(studio, "package.json"), "utf8")) as { dependencies: Record<string, string> };
  const rows = new Map(
    notices
      .split("\n")
      .filter((line) => line.startsWith("| ") && !line.startsWith("| Package") && !line.startsWith("|---"))
      .map((line) => line.split("|").map((cell) => cell.trim()))
      .map(([, name, version, licence]) => [name, { version, licence }] as const),
  );

  it.each(Object.keys(pkg.dependencies))("lists runtime dependency %s with its installed version and licence", (name) => {
    const installed = JSON.parse(fs.readFileSync(path.join(studio, "node_modules", name, "package.json"), "utf8")) as { version: string; license: string };
    const row = rows.get(name);
    expect(row, `${name} is not in a THIRD_PARTY_NOTICES table`).toBeDefined();
    expect(row?.version, `${name} version`).toBe(installed.version);
    expect(row?.licence, `${name} licence`).toContain(installed.license);
  });
});
