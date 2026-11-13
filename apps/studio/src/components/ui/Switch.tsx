"use client";

import React from "react";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import { cn } from "@/lib/cn";
import { focusRing } from "./Button";

/** An on/off switch (`role="switch"`). Give it a name with `aria-label` or a `<label htmlFor>`. */
export const Switch = React.forwardRef<React.ElementRef<typeof SwitchPrimitive.Root>, React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>>(
  function Switch({ className, ...props }, ref) {
    return (
      <SwitchPrimitive.Root
        ref={ref}
        className={cn(
          "peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-border-strong bg-muted p-0.5 transition-colors motion-reduce:transition-none",
          "data-[state=checked]:border-primary data-[state=checked]:bg-primary disabled:cursor-not-allowed disabled:opacity-50",
          focusRing,
          className,
        )}
        {...props}
      >
        <SwitchPrimitive.Thumb
          className={cn(
            "pointer-events-none block size-3.5 rounded-full bg-foreground transition-transform motion-reduce:transition-none",
            "data-[state=checked]:translate-x-4 data-[state=checked]:bg-primary-foreground",
          )}
        />
      </SwitchPrimitive.Root>
    );
  },
);
