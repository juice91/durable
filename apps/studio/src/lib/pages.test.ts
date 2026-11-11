import { test, expect, describe } from "vitest";
import { readFileSync } from "fs";
import { join } from "path";
import { allNavItems } from "@/lib/nav";

describe("Navigation and Pages", () => {
  const appDir = join(__dirname, "../../src/app");

  test("should have a page file for each nav href", () => {
    const missingPages: string[] = [];

    allNavItems.forEach((item) => {
      const href = item.href === "/" ? "/page.tsx" : `${item.href}/page.tsx`;
      const pagePath = join(appDir, href);

      try {
        readFileSync(pagePath, "utf-8");
      } catch (error) {
        missingPages.push(item.href);
      }
    });

    expect(missingPages).toEqual([]);
  });
});
