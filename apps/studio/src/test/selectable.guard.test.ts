/**
 * Every text Studio shows can be selected: `select-none` / `user-select: none` is for controls
 * (menu items, tabs, buttons) and decoration (a diff's line-number gutter), never for content.
 * This scans the source for them: each use must be in the allowlist below, with the reason.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "..");
const PATTERN = /select-none|user-select\s*:\s*none|userSelect\s*:\s*["']none["']/;

/** File → why its `select-none` is not content. */
const ALLOWED: Record<string, string> = {
  "components/ui/Command.tsx": "command palette items are controls",
  "components/ui/DropdownMenu.tsx": "menu items are controls",
  "components/agents/VersionCompare.tsx": "the diff's line-number and +/- gutter is decoration; the line text is selectable",
};

/** Components whose elements carry copyable content: none of them may use it at all. */
const CONTENT_COMPONENTS = [
  "components/workflows/WorkflowRunView.tsx",
  "components/workflows/StepIoPanel.tsx",
  "components/workflows/BranchCases.tsx",
  "components/workflows/WorkflowGraph.tsx",
  "components/workflows/WorkflowSchedules.tsx",
  "components/agentPage/ChatPane.tsx",
  "components/chat/messageParts.tsx",
  "components/approvals/ApprovalCard.tsx",
  "components/traces/PayloadBlock.tsx",
  "components/traces/ExternalTraceSheet.tsx",
  "components/runs/EventTimeline.tsx",
  "components/settings/SecretsSection.tsx",
  "components/apiKeys/ApiKeysSection.tsx",
  "components/ui/CopyableText.tsx",
];

function sources(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.(tsx?|css)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
    }
  };
  walk(SRC);
  return out;
}

describe("selectable content", () => {
  it("only allowlisted files use select-none / user-select: none", () => {
    const hits = sources().filter((f) => PATTERN.test(fs.readFileSync(f, "utf8"))).map((f) => path.relative(SRC, f));
    expect(hits.filter((f) => !(f in ALLOWED))).toEqual([]);
  });

  it("the components that show copyable content never use it", () => {
    for (const rel of CONTENT_COMPONENTS) {
      expect(PATTERN.test(fs.readFileSync(path.join(SRC, rel), "utf8")), `${rel} uses select-none`).toBe(false);
    }
  });
});
