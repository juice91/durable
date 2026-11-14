/**
 * SL.15 layout guard: the phone-width layout rules (plan decision "Phone width and layout rules")
 * checked statically over `src/app` and `src/components`. Rule 4 and `w-screen`/`100vw` are hard
 * failures with no allowlist. The others allow a listed exception with its reason; a stale entry
 * (one that no longer matches anything) fails too, so the list cannot rot.
 *
 * The checks live in `layoutGuard.ts`. Each rule has a fixture violation below that the guard must
 * catch and a compliant twin it must pass.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { checkLayout, isAllowed, type Allowed, type LayoutRule, type Violation } from "./layoutGuard";

const SRC = path.resolve(__dirname, "..");
const ROOTS = ["app", "components"];

/** Test-only sources (tests, harnesses, fixtures) are not app layout. */
const isTestOnly = (rel: string) => /\.test\.tsx?$|Testing\.tsx?$|\.fixtures\.ts$|Fixtures\.ts$/.test(rel);

function sourceFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.(tsx?|css)$/.test(entry.name)) {
        const rel = path.relative(SRC, full).split(path.sep).join("/");
        if (!isTestOnly(rel)) out.push(rel);
      }
    }
  };
  for (const root of ROOTS) walk(path.join(SRC, root));
  return out.sort();
}

/** Known exceptions (never rule 4 or screen), each with its reason. */
const ALLOW: readonly Allowed[] = [
  {
    rule: "R7",
    file: "components/ui/Combobox.tsx",
    token: "<PopoverContent",
    reason: "a menu-sized list (w-72, capped at the available width by Radix), like DropdownMenu; rule 7 is about content popovers",
  },
];

const report = (violations: Violation[]) =>
  violations.map((v) => `${v.rule} ${v.file}:${v.line} ${v.token}\n    ${v.message}`).join("\n");

describe("layout guard over src/app and src/components", () => {
  const files = sourceFiles();
  const all = files.flatMap((file) => checkLayout(file, fs.readFileSync(path.join(SRC, file), "utf8")));

  it("scans the real tree (sanity: pages, components and both stylesheets)", () => {
    expect(files.length).toBeGreaterThan(100);
    expect(files).toContain("app/globals.css");
    expect(files).toContain("components/metrics/chartTheme.css");
    expect(files).toContain("components/frame/AppShell.tsx");
  });

  it("rule 4 (fixed width over 360 px without md:/lg:): none, no exceptions", () => {
    const hard = all.filter((v) => v.rule === "R4");
    expect(hard, report(hard)).toEqual([]);
  });

  it("w-screen / 100vw: none, no exceptions", () => {
    const hard = all.filter((v) => v.rule === "screen");
    expect(hard, report(hard)).toEqual([]);
  });

  it.each(["R1", "R2", "R3", "R5", "R6", "R7"] as const)("%s: no violations outside the listed exceptions", (rule) => {
    const left = all.filter((v) => v.rule === rule && !isAllowed(v, ALLOW));
    expect(left, report(left)).toEqual([]);
  });

  it("every exception still matches a violation and gives a reason", () => {
    for (const entry of ALLOW) {
      expect(entry.reason.length, `${entry.file}`).toBeGreaterThan(20);
      expect(all.some((v) => isAllowed(v, [entry])), `stale exception ${entry.rule} ${entry.file}`).toBe(true);
    }
  });
});

/**
 * One fixture violation per rule, which the guard must catch, and a compliant twin it must pass.
 * [rule, file name (picks TS or CSS checks), violating source, compliant source].
 */
const FIXTURES: Array<[LayoutRule, string, string, string]> = [
  ["R1", "Row.tsx", `<div className="flex"><span className="flex-1 truncate">{title}</span></div>`, `<div className="flex"><span className="min-w-0 flex-1 truncate">{title}</span></div>`],
  ["R2", "Table.tsx", `<table className="w-full"><tbody /></table>`, `<div className="overflow-x-auto"><table className="w-full"><tbody /></table></div>`],
  ["R3", "Kpis.tsx", `<div className="grid grid-cols-4 gap-3" />`, `<div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,11rem),1fr))] md:grid-cols-4" />`],
  ["R4", "Panel.tsx", `<aside className="w-[480px]" />`, `<aside className="w-full md:w-[480px]" />`],
  ["R4", "Panel.tsx", `<aside className="min-w-96" />`, `<aside className="lg:min-w-96" />`],
  ["R4", "Panel.tsx", `<div style={{ width: 400 }} />`, `<div style={{ width: 320 }} />`],
  ["R4", "panel.css", `.panel { width: 30rem; }`, `@media (min-width: 768px) {\n  .panel { width: 30rem; }\n}`],
  ["R5", "Split.tsx", `<div className="grid grid-cols-[22rem_minmax(0,1fr)]" />`, `<div className="grid lg:grid-cols-[22rem_minmax(0,1fr)]" />`],
  ["R6", "Chart.tsx", `<div className="flex"><TimeSeriesChart data={d} /></div>`, `<div className="min-w-0"><TimeSeriesChart data={d} /></div>`],
  ["R7", "Picker.tsx", `<Popover><PopoverContent>…</PopoverContent></Popover>`, `{phone ? <Sheet><SheetContent>…</SheetContent></Sheet> : <Popover><PopoverContent>…</PopoverContent></Popover>}`],
  ["screen", "Wide.tsx", `<div className="w-screen" />`, `<div className="w-full" />`],
  ["screen", "Wide.tsx", `<div className="max-w-[100vw]" />`, `<div className="max-w-full" />`],
  ["screen", "wide.css", `.x { width: 100vw; }`, `.x { width: 100%; }`],
  // SL.15 review M12: what the first version of the guard could not see.
  ["R4", "Panel.tsx", "<aside className={`p-2 ${open ? \"w-[480px]\" : \"w-0\"}`} />", "<aside className={`p-2 ${open ? \"md:w-[480px]\" : \"w-0\"}`} />"],
  ["R4", "Panel.tsx", `<aside className="w-[480px]!" />`, `<aside className="md:w-[480px]!" />`],
  ["R4", "Panel.tsx", `<aside className="!w-96" />`, `<aside className="md:!w-96" />`],
  ["R4", "Panel.tsx", `<div style={{ minWidth: "30em" }} />`, `<div style={{ minWidth: "20em" }} />`],
  ["R4", "Panel.tsx", `<div style={{ inlineSize: 480 }} />`, `<div style={{ inlineSize: 320 }} />`],
  ["R4", "panel.css", `@media (max-width: 1023px) {\n  .panel { width: 30rem; }\n}`, `@media (min-width: 48rem) {\n  .panel { width: 30rem; }\n}`],
  ["R4", "panel.css", `@media (min-width: 640px) {\n  .panel { min-width: 400px; }\n}`, `@media (min-width: 1024px) {\n  .panel { min-width: 400px; }\n}`],
  ["R4", "panel.css", `.panel { inline-size: 25rem; }`, `.panel { inline-size: 20rem; }`],
  ["screen", "Wide.tsx", `<div className="w-[100dvw]" />`, `<div className="w-full" />`],
  ["screen", "Wide.tsx", `<div style={{ width: "100svw" }} />`, `<div style={{ width: "100%" }} />`],
  ["screen", "wide.css", `.x { min-width: 100lvw; }`, `.x { min-width: 100%; }`],
];

describe("layout guard: fixture violations", () => {
  it.each(FIXTURES)("%s in %s is caught, and its compliant twin passes", (rule, file, bad, good) => {
    const caught = checkLayout(file, bad);
    expect(caught.map((v) => v.rule), report(caught)).toContain(rule);
    const clean = checkLayout(file, good).filter((v) => v.rule === rule);
    expect(clean, report(clean)).toEqual([]);
  });

  it("every rule has a fixture", () => {
    expect(new Set(FIXTURES.map(([rule]) => rule))).toEqual(new Set(["R1", "R2", "R3", "R4", "R5", "R6", "R7", "screen"]));
  });

  it("an allowlist entry never excuses rule 4 or screen (the type forbids it; this pins the matcher)", () => {
    const violation = checkLayout("Panel.tsx", `<aside className="w-[480px]" />`)[0];
    const sneaky = { rule: "R4", file: "Panel.tsx", token: "w-", reason: "x".repeat(30) } as unknown as Allowed;
    expect(violation.rule).toBe("R4");
    // The guard's hard-rule assertions ignore ALLOW entirely, so even a matching entry changes nothing.
    expect(isAllowed(violation, [sneaky])).toBeTruthy();
    expect(ALLOW.some((entry) => (entry.rule as string) === "R4" || (entry.rule as string) === "screen")).toBe(false);
  });
});
