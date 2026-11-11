import React from "react";

type IconProps = {
  title: string;
  path: React.ReactNode;
  type?: "line" | "fill";
  className?: string;
};

export function createSvgIcon(title: string, path: React.ReactNode, type: "line" | "fill" = "line") {
  return function Icon({ className }: { className?: string }) {
    return (
      <svg
        className={className}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-label={title}
        xmlns="http://www.w3.org/2000/svg"
      >
        {path}
      </svg>
    );
  };
}

export const DashboardIcon = createSvgIcon(
  "Dashboard",
  <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />,
  "line"
);

export const UsersIcon = createSvgIcon(
  "Users",
  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />,
  "line"
);

export const PlayIcon = createSvgIcon(
  "Play",
  <polygon points="5 3 19 12 5 21 5 3" />,
  "line"
);

export const GitGraphIcon = createSvgIcon(
  "GitGraph",
  <path d="M6 3v12" />,
  "line"
);

export const WrenchIcon = createSvgIcon(
  "Wrench",
  <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />,
  "line"
);

export const CpuIcon = createSvgIcon(
  "Cpu",
  <rect x="4" y="4" width="16" height="16" rx="2" ry="2" />,
  "line"
);

export const BoxIcon = createSvgIcon(
  "Box",
  <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />,
  "line"
);

export const ActivityIcon = createSvgIcon(
  "Activity",
  <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />,
  "line"
);

export const FileTextIcon = createSvgIcon(
  "FileText",
  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />,
  "line"
);

export const BarChartIcon = createSvgIcon(
  "BarChart",
  <line x1="18" y1="20" x2="18" y2="10" />,
  "line"
);

export const SettingsIcon = createSvgIcon(
  "Settings",
  <circle cx="12" cy="12" r="3" />,
  "line"
);
