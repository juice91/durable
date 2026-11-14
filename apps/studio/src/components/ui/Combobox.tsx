"use client";

import React, { useState } from "react";
import { Check, ChevronsUpDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button } from "./Button";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "./Command";
import { Popover, PopoverContent, PopoverTrigger } from "./Popover";

export interface ComboboxOption {
  value: string;
  label: string;
  /** Second line under the label. */
  description?: string;
  /** Extra words that match the search. */
  keywords?: string[];
  disabled?: boolean;
}

export interface ComboboxProps {
  /** Accessible name of the trigger ("Switch agent"). */
  label: string;
  options: ComboboxOption[];
  value: string | null;
  onValueChange: (value: string) => void;
  /** Trigger text when nothing is selected. */
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  /** Custom trigger content; defaults to the selected option's label. */
  children?: React.ReactNode;
  disabled?: boolean;
  className?: string;
  contentClassName?: string;
  /** data-testid on the trigger. */
  testId?: string;
}

/**
 * A searchable select (the breadcrumb entity switcher). The trigger is a `combobox` button; the list
 * opens in a Popover with a search box, arrow keys and Enter. Escape closes it and focus returns to
 * the trigger.
 */
export function Combobox({
  label,
  options,
  value,
  onValueChange,
  placeholder = "Select…",
  searchPlaceholder = "Search…",
  emptyText = "No matching results.",
  children,
  disabled,
  className,
  contentClassName,
  testId,
}: ComboboxProps) {
  const [open, setOpen] = useState(false);
  const selected = options.find((option) => option.value === value) ?? null;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          role="combobox"
          aria-expanded={open}
          aria-label={label}
          variant="ghost"
          disabled={disabled}
          data-testid={testId}
          className={cn("min-w-0 justify-between gap-1 px-2", className)}
        >
          <span className="min-w-0 truncate">{children ?? selected?.label ?? placeholder}</span>
          <ChevronsUpDown className="text-muted-foreground" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent aria-label={label} className={cn("w-72 p-0", contentClassName)}>
        <Command loop>
          <CommandInput placeholder={searchPlaceholder} aria-label={searchPlaceholder} />
          <CommandList>
            <CommandEmpty>{emptyText}</CommandEmpty>
            <CommandGroup>
              {options.map((option) => (
                <CommandItem
                  key={option.value}
                  value={option.value}
                  keywords={[option.label, ...(option.keywords ?? [])]}
                  disabled={option.disabled}
                  onSelect={() => {
                    onValueChange(option.value);
                    setOpen(false);
                  }}
                >
                  <Check className={cn(option.value === value ? "opacity-100" : "opacity-0")} strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate">{option.label}</span>
                    {option.description ? <span className="truncate text-caption text-muted-foreground">{option.description}</span> : null}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
