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
