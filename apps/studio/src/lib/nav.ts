import { DashboardIcon, UsersIcon, PlayIcon, GitGraphIcon, WrenchIcon, CpuIcon, BoxIcon, ActivityIcon, FileTextIcon, SettingsIcon, BarChartIcon } from "./icons";

export type NavItem = {
  label: string;
  href: string;
  icon: React.FC<{ className?: string }>;
};

export type NavSection = {
  title: string;
  items: NavItem[];
};

export const navSections: NavSection[] = [
  {
    title: "Build",
    items: [
      { label: "Dashboard", href: "/", icon: DashboardIcon },
      { label: "Agents", href: "/agents", icon: UsersIcon },
      { label: "Playground", href: "/playground", icon: PlayIcon },
      { label: "Workflows", href: "/workflows", icon: GitGraphIcon },
      { label: "Tools", href: "/tools", icon: WrenchIcon },
      { label: "MCP Servers", href: "/mcp", icon: CpuIcon },
      { label: "Processors", href: "/processors", icon: BoxIcon },
      { label: "Request Context", href: "/request-context", icon: FileTextIcon },
    ],
  },
  {
    title: "Observe",
    items: [
      { label: "Traces", href: "/traces", icon: ActivityIcon },
      { label: "Logs", href: "/logs", icon: FileTextIcon },
      { label: "Metrics", href: "/metrics", icon: BarChartIcon },
    ],
  },
  {
    title: "Settings",
    items: [
      { label: "Settings", href: "/settings", icon: SettingsIcon },
    ],
  },
];

export const allNavItems: NavItem[] = navSections.flatMap((section) => section.items);
