import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// Studio's own scales from globals.css `@theme`. Without them tailwind-merge reads `text-label` as a
// text colour and drops it next to `text-foreground`, and reads `h-row` as an arbitrary value.
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      text: ["badge", "caption", "label", "body", "title", "display"],
      shadow: ["raised", "overlay"],
      spacing: ["header", "row", "row-compact", "table-header", "sidebar-item", "icon", "icon-dense", "target"],
    },
  },
});

/** Joins class names and lets later Tailwind utilities override earlier conflicting ones. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
