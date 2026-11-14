"use client";

import React from "react";
import { cn } from "@/lib/cn";
import { shortcutSections, type ShortcutRow, type ShortcutSection } from "@/lib/shortcuts";
import { Kbd } from "@/components/ui/Kbd";
import { usePaletteIsMac } from "./usePaletteChordLabel";

function Keys({ row }: { row: ShortcutRow }) {
  return (
    <span className="inline-flex flex-wrap items-center justify-end gap-1">
      {row.keys.map((combo, index) => (
        <React.Fragment key={combo.join("+")}>
          {index > 0 ? <span className="text-caption text-muted-foreground">{row.sequence ? "then" : "or"}</span> : null}
          <span className="inline-flex items-center gap-0.5">
            {combo.map((key, keyIndex) => (
              <React.Fragment key={`${key}-${keyIndex}`}>
                {row.sequence && keyIndex > 0 ? <span className="px-0.5 text-caption text-muted-foreground">then</span> : null}
                <Kbd>{key}</Kbd>
              </React.Fragment>
            ))}
          </span>
        </React.Fragment>
      ))}
    </span>
  );
}

export interface ShortcutTableProps {
  sections?: ShortcutSection[];
  /** Heading level of each section title (the help dialog uses 3 under its title, Settings 3 under its h2). */
  headingLevel?: 2 | 3 | 4;
  className?: string;
}

/**
 * The keyboard shortcut list, one table per section (General, Go to). Shared by the help dialog
 * (`?`) and the Settings page, so both always show the same keys.
 */
export function ShortcutTable({ sections: given, headingLevel = 3, className }: ShortcutTableProps) {
  // The palette chord row shows only the chord that works here (⌘K on macOS, Ctrl K elsewhere).
  const mac = usePaletteIsMac();
  const sections = given ?? shortcutSections(undefined, mac);
  const Heading = `h${headingLevel}` as "h3";
  return (
    <div data-testid="shortcut-table" className={cn("flex min-w-0 flex-col gap-4", className)}>
      {sections.map((section) => (
        <section key={section.id} aria-labelledby={`shortcuts-${section.id}`} className="min-w-0">
          <Heading id={`shortcuts-${section.id}`} className="pb-1 text-caption font-medium text-muted-foreground">
            {section.title}
          </Heading>
          <div className="min-w-0 overflow-x-auto">
            <table className="w-full text-label">
              <thead className="sr-only">
                <tr>
                  <th scope="col">Action</th>
                  <th scope="col">Keys</th>
                </tr>
              </thead>
              <tbody>
                {section.rows.map((row) => (
                  <tr key={row.id} data-shortcut-row={row.id} className="border-b border-border last:border-b-0">
                    <td className="py-1.5 pr-3 text-foreground">{row.label}</td>
                    <td className="py-1.5 text-right">
                      <Keys row={row} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  );
}
