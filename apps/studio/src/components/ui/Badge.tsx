import React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

export const badgeVariants = cva("inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-sm px-1.5 py-0.5 text-badge font-medium [&_svg]:size-3", {
  variants: {
    variant: {
      neutral: "bg-muted text-muted-foreground",
      outline: "border border-border text-foreground",
      success: "bg-success/10 text-success-text",
      warning: "bg-warning/10 text-warning-text",
      info: "bg-info/10 text-info-text",
      destructive: "bg-destructive/10 text-destructive-text",
    },
  },
  defaultVariants: { variant: "neutral" },
});

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
