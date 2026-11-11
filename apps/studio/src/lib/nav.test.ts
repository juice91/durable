import { test, expect, describe } from "vitest";
import { navSections, allNavItems } from "@/lib/nav";

describe("Navigation", () => {
  describe("Nav Items", () => {
    test("should have unique hrefs", () => {
      const hrefs = allNavItems.map((item) => item.href);
      const uniqueHrefs = new Set(hrefs);
      expect(uniqueHrefs.size).toBe(hrefs.length);
    });

    test("should have unique labels", () => {
      const labels = allNavItems.map((item) => item.label);
      const uniqueLabels = new Set(labels);
      expect(uniqueLabels.size).toBe(labels.length);
    });

    test("should have at least one item in each section", () => {
      navSections.forEach((section) => {
        expect(section.items.length).toBeGreaterThan(0);
      });
    });
  });
});
