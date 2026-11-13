import { test, expect, describe } from "vitest";
import React from "react";
import { render } from "@testing-library/react";
import {
  activeNavItem,
  allNavItems,
  isNavItemActive,
  NAV_GROUP_ORDER,
  NAV_GROUPS,
  navCrumb,
  navItemForGKey,
  navItems,
  navSections,
  navSectionsFor,
  type NavItem,
} from "@/lib/nav";
import { routes } from "@/lib/routes";
import { AgentsIcon, GitGraphIcon } from "@/lib/icons";

const byId = (id: string): NavItem => {
  const item = navItems.find((candidate) => candidate.id === id);
  if (!item) throw new Error(`no nav item ${id}`);
  return item;
};

describe("Navigation", () => {
  describe("Nav Items", () => {
    test("should have unique hrefs", () => {
      const hrefs = allNavItems.map((item) => item.href);
      expect(new Set(hrefs).size).toBe(hrefs.length);
    });

    test("should have unique labels", () => {
      const labels = allNavItems.map((item) => item.label);
      expect(new Set(labels).size).toBe(labels.length);
    });

    test("should have at least one item in each section", () => {
      navSections.forEach((section) => {
        expect(section.items.length).toBeGreaterThan(0);
      });
    });

    test("ids and g-keys are unique", () => {
      const ids = navItems.map((item) => item.id);
      expect(new Set(ids).size).toBe(ids.length);
      const gKeys = navItems.flatMap((item) => (item.gKey ? [item.gKey] : []));
      expect(new Set(gKeys).size).toBe(gKeys.length);
    });

    test("every item belongs to a known group, and allNavItems covers the registry", () => {
      for (const item of navItems) expect(NAV_GROUP_ORDER).toContain(item.group);
      expect([...allNavItems].map((item) => item.id).sort()).toEqual([...navItems].map((item) => item.id).sort());
    });

    test("groups and items render in the planned order; empty groups are left out", () => {
      expect(NAV_GROUPS.map((group) => group.id)).toEqual(["approvals", "primitives", "evaluation", "observability", "settings"]);
      expect(navSections.map((section) => section.group.id)).toEqual(["primitives", "observability", "settings"]);
      expect(navSections.map((section) => section.group.title)).toEqual(["Primitives", "Observability", null]);
      const permanent = (groupId: string) =>
        navSections.find((section) => section.group.id === groupId)!.items.filter((item) => !item.transitional).map((item) => item.label);
      expect(permanent("primitives")).toEqual(["Agents", "Workflows", "Tools", "MCP Servers", "Processors"]);
      expect(permanent("observability")).toEqual(["Metrics", "Traces", "Logs"]);
      expect(permanent("settings")).toEqual(["Settings"]);
      expect(navSections.find((section) => section.group.id === "settings")!.group.placement).toBe("footer");
    });

    test("the transitional item is exactly Request Context, with no g-key (SL.6 removed Playground, SL.7 Dashboard)", () => {
      const transitional = navItems.filter((item) => item.transitional);
      expect(transitional.map((item) => item.id).sort()).toEqual(["request-context"]);
      for (const item of transitional) expect(item.gKey).toBeUndefined();
    });

    test("g-keys follow the plan", () => {
      const keys = Object.fromEntries(navItems.filter((item) => item.gKey).map((item) => [item.gKey, item.id]));
      expect(keys).toEqual({ a: "agents", w: "workflows", o: "tools", m: "mcp", p: "processors", n: "metrics", t: "traces", l: "logs", ",": "settings" });
      expect(navItemForGKey("t")?.href).toBe(routes.traces());
      expect(navItemForGKey("z")).toBeNull();
    });

    test("navSectionsFor drops a group whose items are all gone", () => {
      const sections = navSectionsFor(navItems.filter((item) => item.group !== "observability"));
      expect(sections.map((section) => section.group.id)).toEqual(["primitives", "settings"]);
    });
  });

  describe("navCrumb", () => {
    test("builds a crumb from the registry: a link, or the current page's plain title", () => {
      expect(navCrumb("agents")).toEqual({ label: "Agents", href: routes.agents() });
      expect(navCrumb("mcp", { current: true })).toEqual({ label: "MCP Servers" });
      for (const item of navItems) expect(navCrumb(item.id)).toEqual({ label: item.label, href: item.href });
    });

    test("an unknown id throws", () => {
      expect(() => navCrumb("nope")).toThrow('Unknown nav item "nope"');
    });
  });

  describe("Active state", () => {
    test("an item is active on its href and every sub-path", () => {
      const agents = byId("agents");
      expect(isNavItemActive(agents, routes.agents())).toBe(true);
      expect(isNavItemActive(agents, routes.agentEditor("a1"))).toBe(true);
      expect(isNavItemActive(agents, "/agents/a1/chat/c1")).toBe(true);
      expect(isNavItemActive(agents, routes.agentNew())).toBe(true);
    });

    test("a prefix only matches whole segments, and query strings are ignored", () => {
      const agents = byId("agents");
      expect(isNavItemActive(agents, "/agentsx")).toBe(false);
      expect(isNavItemActive(agents, "/agents?q=1")).toBe(true);
      expect(isNavItemActive(agents, null)).toBe(false);
    });

    test("no item is active on / (a redirect since SL.7); Metrics carries the dashboard's palette words", () => {
      expect(navItems.some((item) => item.id === "dashboard")).toBe(false);
      expect(activeNavItem("/")).toBeNull();
      expect(isNavItemActive(byId("metrics"), "/")).toBe(false);
      expect(byId("metrics").keywords).toEqual(expect.arrayContaining(["overview", "dashboard"]));
    });

    test("run pages activate Traces; workflow runs activate Workflows", () => {
      expect(activeNavItem(routes.run("r1"))?.id).toBe("traces");
      expect(activeNavItem(routes.trace("r1"))?.id).toBe("traces");
      expect(activeNavItem(routes.workflowRun("w1", "r1"))?.id).toBe("workflows");
    });

    test("exactly one item is active on each section's pages, and none on an unknown path", () => {
      for (const item of navItems) {
        const active = navItems.filter((candidate) => isNavItemActive(candidate, item.href));
        expect(active.map((candidate) => candidate.id)).toEqual([item.id]);
      }
      expect(activeNavItem("/nowhere")).toBeNull();
    });
  });

  describe("Icons", () => {
    test("should render as SVG elements", () => {
      navItems.forEach((item) => {
        const Icon = item.icon;
        const { container } = render(<Icon className="w-4 h-4" />);
        expect(container.querySelector("svg")).toBeInTheDocument();
      });
      const { container } = render(<AgentsIcon className="w-4 h-4" />);
      expect(container.querySelector("svg")).toBeInTheDocument();
    });

    test("should have proper viewBox", () => {
      const { container } = render(<GitGraphIcon className="w-4 h-4" />);
      expect(container.querySelector("svg")).toHaveAttribute("viewBox", "0 0 24 24");
    });
  });
});
