import { test, expect, describe } from "vitest";
import React from "react";
import { navSections, allNavItems } from "@/lib/nav";
import {
  DashboardIcon,
  UsersIcon,
  PlayIcon,
  GitGraphIcon,
  WrenchIcon,
  CpuIcon,
  BoxIcon,
  ActivityIcon,
  FileTextIcon,
  SettingsIcon,
  BarChartIcon,
} from "@/lib/icons";
import { render } from "@testing-library/react";

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

  describe("Icons", () => {
    test("should render as SVG elements", () => {
      const icons = [
        DashboardIcon,
        UsersIcon,
        PlayIcon,
        GitGraphIcon,
        WrenchIcon,
        CpuIcon,
        BoxIcon,
        ActivityIcon,
        FileTextIcon,
        SettingsIcon,
        BarChartIcon,
      ];

      icons.forEach((Icon) => {
        const { container } = render(<Icon className="w-4 h-4" />);
        const svg = container.querySelector("svg");
        expect(svg).toBeInTheDocument();
      });
    });

    test("should have proper viewBox", () => {
      const { container } = render(<GitGraphIcon className="w-4 h-4" />);
      const svg = container.querySelector("svg");
      expect(svg).toHaveAttribute("viewBox", "0 0 24 24");
    });
  });
});
