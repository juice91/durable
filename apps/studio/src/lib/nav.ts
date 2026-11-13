import { DashboardIcon, UsersIcon, PlayIcon, GitGraphIcon, WrenchIcon, CpuIcon, BoxIcon, ActivityIcon, FileTextIcon, SettingsIcon, BarChartIcon } from "./icons";
import { routes } from "./routes";

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
      { label: "Dashboard", href: routes.dashboard(), icon: DashboardIcon },
      { label: "Agents", href: routes.agents(), icon: UsersIcon },
      { label: "Playground", href: routes.playground(), icon: PlayIcon },
      { label: "Workflows", href: routes.workflows(), icon: GitGraphIcon },
      { label: "Tools", href: routes.tools(), icon: WrenchIcon },
      { label: "MCP Servers", href: routes.mcp(), icon: CpuIcon },
      { label: "Processors", href: routes.processors(), icon: BoxIcon },
      { label: "Request Context", href: routes.requestContext(), icon: FileTextIcon },
    ],
  },
  {
    title: "Observe",
    items: [
      { label: "Traces", href: routes.traces(), icon: ActivityIcon },
      { label: "Logs", href: routes.logs(), icon: FileTextIcon },
      { label: "Metrics", href: routes.metrics(), icon: BarChartIcon },
    ],
  },
  {
    title: "Settings",
    items: [
      { label: "Settings", href: routes.settings(), icon: SettingsIcon },
    ],
  },
];

export const allNavItems: NavItem[] = navSections.flatMap((section) => section.items);
