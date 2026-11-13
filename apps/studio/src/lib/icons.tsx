import React from "react";
import {
  BookOpen,
  Bot,
  ChartLine,
  FileText,
  Inbox,
  KeyRound,
  LayoutDashboard,
  ListTree,
  Play,
  Plug,
  ScrollText,
  Settings,
  ShieldCheck,
  Workflow,
  Wrench,
  type LucideIcon,
  type LucideProps,
} from "lucide-react";

/** Studio draws every icon with this stroke width. */
export const ICON_STROKE_WIDTH = 1.75;

export type IconProps = LucideProps;
export type IconComponent = React.ForwardRefExoticComponent<IconProps & React.RefAttributes<SVGSVGElement>>;

/**
 * Wraps a lucide icon so it defaults to Studio's stroke width. Icons are decorative
 * (`aria-hidden`) unless given an `aria-label` or `title`, which is lucide's own rule.
 */
export function studioIcon(Icon: LucideIcon, displayName: string): IconComponent {
  const Wrapped = React.forwardRef<SVGSVGElement, IconProps>(function StudioIcon({ strokeWidth = ICON_STROKE_WIDTH, ...props }, ref) {
    return <Icon ref={ref} strokeWidth={strokeWidth} {...props} />;
  });
  Wrapped.displayName = displayName;
  return Wrapped;
}

// Section icons, by the plan's names.
export const AgentsIcon = studioIcon(Bot, "AgentsIcon");
export const WorkflowsIcon = studioIcon(Workflow, "WorkflowsIcon");
export const ToolsIcon = studioIcon(Wrench, "ToolsIcon");
export const McpIcon = studioIcon(Plug, "McpIcon");
export const ProcessorsIcon = studioIcon(ShieldCheck, "ProcessorsIcon");
export const KnowledgeIcon = studioIcon(BookOpen, "KnowledgeIcon");
export const MetricsIcon = studioIcon(ChartLine, "MetricsIcon");
export const TracesIcon = studioIcon(ListTree, "TracesIcon");
export const LogsIcon = studioIcon(ScrollText, "LogsIcon");
export const SettingsIcon = studioIcon(Settings, "SettingsIcon");
export const ApprovalsIcon = studioIcon(Inbox, "ApprovalsIcon");
// Transitional pages (removed with their nav items in SL.6, SL.7 and SL.10).
export const DashboardIcon = studioIcon(LayoutDashboard, "DashboardIcon");
export const PlaygroundIcon = studioIcon(Play, "PlaygroundIcon");
export const RequestContextIcon = studioIcon(KeyRound, "RequestContextIcon");

// Earlier names, kept so existing imports compile. Each maps to its section's lucide icon.
export const UsersIcon = AgentsIcon;
export const PlayIcon = PlaygroundIcon;
export const GitGraphIcon = WorkflowsIcon;
export const WrenchIcon = ToolsIcon;
export const CpuIcon = McpIcon;
export const BoxIcon = ProcessorsIcon;
export const ActivityIcon = TracesIcon;
export const FileTextIcon = studioIcon(FileText, "FileTextIcon");
export const BarChartIcon = MetricsIcon;
