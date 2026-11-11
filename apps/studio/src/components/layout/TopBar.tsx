import React from "react";

export function TopBar() {
  return (
    <header className="h-16 border-b border-border bg-background px-4 flex items-center justify-between">
      <div className="flex items-center">
        <span className="text-lg font-semibold text-foreground">AI Platform Studio</span>
      </div>
      <div className="flex items-center space-x-4">
        <div className="h-8 w-8 rounded-full bg-accent flex items-center justify-center text-accent-foreground text-sm font-medium">
          US
        </div>
      </div>
    </header>
  );
}
