"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Columns3 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/DropdownMenu";
import { ICON_STROKE_WIDTH } from "@/lib/icons";

export interface ChooserColumn {
  id: string;
  label: string;
  /** A column the viewer cannot hide (the row's link). */
  locked?: boolean;
}

function readHidden(storageKey: string, fallback: string[]): string[] {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (raw === null) return fallback;
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === "string") : fallback;
  } catch {
    return fallback;
  }
}

/**
 * Hidden column ids for a list, persisted per viewer in localStorage under `storageKey` (every
 * read and write is guarded: private windows and blocked storage just forget the choice).
 */
export function useColumnVisibility(storageKey: string, defaultHidden: readonly string[] = []) {
  const initial = defaultHidden.join("\u0000");
  const [hidden, setHidden] = useState<string[]>([...defaultHidden]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setHidden(readHidden(storageKey, initial ? initial.split("\u0000") : [])), [storageKey, initial]);
  const setVisible = useCallback(
    (id: string, visible: boolean) => {
      setHidden((prev) => {
        const next = visible ? prev.filter((h) => h !== id) : prev.includes(id) ? prev : [...prev, id];
        try {
          window.localStorage.setItem(storageKey, JSON.stringify(next));
        } catch {
          // Storage unavailable: the choice lasts until reload.
        }
        return next;
      });
    },
    [storageKey],
  );
  const reset = useCallback(() => {
    setHidden([]);
    try {
      window.localStorage.setItem(storageKey, "[]");
    } catch {
      // Storage unavailable.
    }
  }, [storageKey]);
  return { hidden, isVisible: (id: string) => !hidden.includes(id), setVisible, reset };
}

/** A "Columns" menu with one checkbox per column. */
export function ColumnChooser({ columns, hidden, onVisibleChange, onReset }: {
  columns: ChooserColumn[];
  hidden: string[];
  onVisibleChange: (id: string, visible: boolean) => void;
  onReset?: () => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="secondary" size="sm" data-testid="column-chooser">
          <Columns3 className="size-icon" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          Columns
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" aria-label="Choose columns">
        <DropdownMenuLabel>Show columns</DropdownMenuLabel>
        {columns.map((column) => (
          <DropdownMenuCheckboxItem
            key={column.id}
            checked={column.locked ? true : !hidden.includes(column.id)}
            disabled={column.locked}
            onSelect={(e) => e.preventDefault()}
            onCheckedChange={(checked) => onVisibleChange(column.id, checked === true)}
          >
            {column.label}
          </DropdownMenuCheckboxItem>
        ))}
        {onReset ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={(e) => { e.preventDefault(); onReset(); }}>Show all columns</DropdownMenuItem>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
