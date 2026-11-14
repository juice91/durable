import { test, expect, describe } from "vitest";
import { existsSync } from "fs";
import { join } from "path";
import { allNavItems, navItems } from "@/lib/nav";

describe("Navigation and Pages", () => {
  const appDir = join(__dirname, "../../src/app");
  const pageFor = (href: string) => join(appDir, href === "/" ? "/page.tsx" : `${href.split(/[?#]/)[0]}/page.tsx`);

  test("should have a page file for each nav href", () => {
    const missingPages = allNavItems.filter((item) => !existsSync(pageFor(item.href))).map((item) => item.href);
    expect(missingPages).toEqual([]);
  });

  test("every non-transitional item has its own page (transitional ones may become redirects)", () => {
    const permanent = navItems.filter((item) => !item.transitional);
    expect(permanent.length).toBeGreaterThan(0);
    expect(permanent.filter((item) => !existsSync(pageFor(item.href))).map((item) => item.id)).toEqual([]);
  });
});
