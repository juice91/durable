import React from "react";
import { Info } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";

export interface EmptyStateProps {
  title: string;
  description?: React.ReactNode;
  /** Decorative icon; defaults to an info mark. */
  icon?: React.ReactNode;
  /** A primary action ("New agent"). */
  action?: React.ReactNode;
  /** Tighter padding, for empty states inside cards, sheets and lists. */
  compact?: boolean;
  className?: string;
  testId?: string;
}

/** A centred "nothing here yet" block: icon, heading (h2), optional description and action. */
export function EmptyState({ title, description, icon, action, compact = false, className, testId }: EmptyStateProps) {
  return (
    <div data-testid={testId} className={cn("flex min-w-0 flex-col items-center justify-center px-4 text-center", compact ? "py-8" : "py-16", className)}>
      <div aria-hidden="true" className="mb-3 flex size-10 items-center justify-center rounded-full bg-muted text-muted-foreground [&_svg]:size-5">
        {icon ?? <Info strokeWidth={ICON_STROKE_WIDTH} />}
      </div>
      <h2 className="mb-1 text-title font-semibold text-foreground">{title}</h2>
      {description ? <p className="max-w-md text-body text-muted-foreground">{description}</p> : null}
      {action ? <div className="mt-4 flex flex-wrap justify-center gap-2">{action}</div> : null}
    </div>
  );
}
