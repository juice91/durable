import { afterEach, describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { AGENT_CONFIG_KEY_PREFIX, AGENTS_VIEW_KEY, LAST_AGENT_KEY, agentLayoutKey, writeConfigOpen } from "@/components/agentPage/agentPageModel";
import { SIDE_PANEL_STORAGE_KEY } from "@/components/frame/frameStorage";
import { SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";
import { THEME_STORAGE_KEY } from "@/lib/theme";
import { BROWSER_DATA, clearBrowserData } from "./browserData";

const SRC = path.resolve(__dirname, "../..");

function sourceFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(full);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name) ? [full] : [];
  });
}

/**
 * Whether Settings lists `key`: an exact key, or a key under a registered prefix. A template head
 * (`templateHead`: the capture is directly followed by `${`) may also be the start of a prefix.
 */
function registered(key: string, templateHead = false): boolean {
  return BROWSER_DATA.some(
    (entry) =>
      entry.keys.includes(key) ||
      entry.prefixes.some((prefix) => key.startsWith(prefix) || (templateHead && prefix.startsWith(key))),
  );
}

afterEach(() => window.localStorage.clear());

describe("BROWSER_DATA covers every studio.* key", () => {
  it("lists the frame, Settings and agent page keys", () => {
    for (const key of [THEME_STORAGE_KEY, SIDEBAR_STORAGE_KEY, SIDE_PANEL_STORAGE_KEY, LAST_AGENT_KEY, AGENTS_VIEW_KEY, agentLayoutKey("a1"), `${AGENT_CONFIG_KEY_PREFIX}a1`]) {
      expect(registered(key), key).toBe(true);
    }
  });

  it("every `studio.` string in Studio's source is a key Settings lists", () => {
    const found = new Map<string, { file: string; templateHead: boolean }>();
    const dynamicHeads: string[] = [];
    for (const file of sourceFiles(SRC)) {
      const text = fs.readFileSync(file, "utf8");
      for (const match of text.matchAll(/["'`[](studio\.[A-Za-z0-9_.-]+)(\$\{)?/g)) {
        found.set(match[1], { file: path.relative(SRC, file), templateHead: match[2] !== undefined });
      }
      // A key whose name is built right after `studio.` ("studio." + x, `studio.${x}`) can't be checked.
      if (/["'`]studio\.(["'`]|\$\{)/.test(text)) dynamicHeads.push(path.relative(SRC, file));
    }
    expect(found.size).toBeGreaterThan(5);
    expect(dynamicHeads).toEqual([]);
    const missing = [...found]
      .filter(([key, { templateHead }]) => !registered(key, templateHead))
      .map(([key, { file }]) => `${key} (${file})`);
    expect(missing).toEqual([]);
  });
});

describe("Clear layout preferences", () => {
  it("removes the agent Config preference and the agents view with the pane sizes, and nothing else", () => {
    writeConfigOpen("a1", false);
    window.localStorage.setItem(AGENTS_VIEW_KEY, "compact");
    window.localStorage.setItem(agentLayoutKey("a1"), "{}");
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    window.localStorage.setItem("unrelated", "x");
    const result = clearBrowserData("layout");
    expect(result?.removed).toEqual(expect.arrayContaining([`${AGENT_CONFIG_KEY_PREFIX}a1`, AGENTS_VIEW_KEY, agentLayoutKey("a1")]));
    expect(window.localStorage.getItem(`${AGENT_CONFIG_KEY_PREFIX}a1`)).toBeNull();
    expect(window.localStorage.getItem(AGENTS_VIEW_KEY)).toBeNull();
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(window.localStorage.getItem("unrelated")).toBe("x");
  });
});
