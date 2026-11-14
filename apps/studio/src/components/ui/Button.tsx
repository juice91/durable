"use client";

import React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

/** Shared focus ring for every interactive element in the kit. */
export const focusRing = "outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background";

export const buttonVariants = cva(
  cn(
    "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-md font-medium transition-colors motion-reduce:transition-none",
    "disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-icon [&_svg]:shrink-0",
    focusRing,
  ),
  {
    variants: {
      variant: {
        primary: "bg-primary text-primary-foreground hover:bg-primary/90",
        secondary: "border border-border bg-surface text-foreground hover:bg-fill-hover active:bg-fill-active",
        ghost: "text-foreground hover:bg-fill-hover active:bg-fill-active",
        destructive: "bg-destructive text-destructive-foreground hover:bg-destructive/90",
        link: "h-auto px-0 text-foreground underline-offset-4 hover:underline",
      },
      size: {
        sm: "h-7 px-2 text-label",
        md: "h-8 px-3 text-label",
        lg: "h-9 px-4 text-body",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {
  /** Render the single child (a link, say) with button styles instead of a <button>. */
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button({ className, variant, size, asChild = false, type, ...props }, ref) {
  const Component = asChild ? Slot : "button";
  return <Component ref={ref} type={asChild ? type : (type ?? "button")} className={cn(buttonVariants({ variant, size }), className)} {...props} />;
});
