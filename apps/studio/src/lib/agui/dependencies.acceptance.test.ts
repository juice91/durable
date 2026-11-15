import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const studioDir = resolve(__dirname, "../../..");
const pkg = JSON.parse(readFileSync(resolve(studioDir, "package.json"), "utf8")) as {
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
};
const lock = readFileSync(resolve(studioDir, "../../pnpm-lock.yaml"), "utf8");

/** Versions resolved in the lockfile `packages:` section (top-level keys, peer suffix stripped). */
function lockVersions(name: string): string[] {
  const section = lock.slice(lock.indexOf("\npackages:\n"), lock.indexOf("\nsnapshots:\n"));
  const versions = new Set<string>();
  for (const line of section.split("\n")) {
    const m = /^ {2}'?([^'@\s][^'@]*|@[^'@/]+\/[^'@]+)@([^':(]+)/.exec(line);
    if (m && m[1] === name) versions.add(m[2]);
  }
  return [...versions];
}

describe("AGUI.11 exact pins in package.json", () => {
  const deps = { ...pkg.devDependencies, ...pkg.dependencies };
  it.each([
    ["@assistant-ui/react-ag-ui", "0.1.1"],
    ["@ag-ui/client", "1.0.3"],
    ["@ag-ui/core", "1.0.3"],
  ])("%s is pinned exactly to %s", (name, version) => {
    expect(deps[name]).toBe(version);
  });
});

describe("AGUI.11 lockfile resolves one version", () => {
  it.each([
    ["@assistant-ui/react-ag-ui", "0.1.1"],
    ["@ag-ui/client", "1.0.3"],
    ["@ag-ui/core", "1.0.3"],
  ])("%s resolves to %s", (name, version) => {
    expect(lockVersions(name)).toEqual([version]);
  });

  it.each(["@assistant-ui/core", "@assistant-ui/store", "@ag-ui/client", "@ag-ui/core"])(
    "%s has exactly one resolved version",
    (name) => {
      expect(lockVersions(name)).toHaveLength(1);
    },
  );

  it("an absent package resolves to no versions", () => {
    expect(lockVersions("no-such-package")).toEqual([]);
  });
});
