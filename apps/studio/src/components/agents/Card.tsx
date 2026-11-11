import React from "react";

export function Card({ children, className = "", onClick, tabIndex, onKeyDown }: React.HTMLAttributes<HTMLDivElement> & { onClick?: () => void; tabIndex?: number; onKeyDown?: (e: React.KeyboardEvent) => void }) {
  return (
    <div
      className={`bg-white border border-gray-200 rounded-md shadow-sm ${className}`}
      onClick={onClick}
      tabIndex={tabIndex}
      onKeyDown={onKeyDown}
    >
      {children}
    </div>
  );
}
