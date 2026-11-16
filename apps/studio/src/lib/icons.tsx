import React from "react";
import {
  BookOpen,
  Bot,
  ChartLine,
  Database,
  FlaskConical,
  Gauge,
  Inbox,
  KeyRound,
  ListTree,
  MessagesSquare,
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
export const ThreadsIcon = studioIcon(MessagesSquare, "ThreadsIcon");
export const LogsIcon = studioIcon(ScrollText, "LogsIcon");
export const DatasetsIcon = studioIcon(Database, "DatasetsIcon");
export const ScorersIcon = studioIcon(Gauge, "ScorersIcon");
export const ExperimentsIcon = studioIcon(FlaskConical, "ExperimentsIcon");
export const SettingsIcon = studioIcon(Settings, "SettingsIcon");
export const ApprovalsIcon = studioIcon(Inbox, "ApprovalsIcon");
// Transitional page (removed with its nav item in SL.10).
export const RequestContextIcon = studioIcon(KeyRound, "RequestContextIcon");
