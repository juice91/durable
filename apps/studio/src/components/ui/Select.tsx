"use client";

import React from "react";
import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { focusRing } from "./Button";

/** Radix forbids an item with value "", so an `<option value="">` travels under this stand-in. */
const EMPTY = "__select-empty__";
const toInternal = (value: string) => (value === "" ? EMPTY : value);
const fromInternal = (value: string) => (value === EMPTY ? "" : value);

export interface SelectOptionData {
  value: string;
  label: React.ReactNode;
  disabled: boolean;
}

/** Reads `<option>` children (fragments and arrays included) into a flat list. */
export function readOptions(children: React.ReactNode): SelectOptionData[] {
  const out: SelectOptionData[] = [];
  const visit = (node: React.ReactNode) => {
    React.Children.forEach(node, (child) => {
      if (!React.isValidElement(child)) return;
      const props = child.props as { value?: string | number; disabled?: boolean; children?: React.ReactNode };
      if (child.type === React.Fragment) return visit(props.children);
      if (child.type === "option") {
        const label = props.children ?? "";
        out.push({ value: String(props.value ?? textOf(label)), label, disabled: Boolean(props.disabled) });
      }
    });
  };
  visit(children);
  return out;
}

function textOf(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (React.isValidElement(node)) return textOf((node.props as { children?: React.ReactNode }).children);
  return "";
}

const triggerSizes = {
  sm: "h-7 px-2 text-label",
  md: "h-8 px-3 text-label",
  lg: "h-9 px-4 text-body",
} as const;

export interface SelectProps {
  /** `<option value>` elements, as for a native select. */
  children?: React.ReactNode;
  value?: string;
  defaultValue?: string;
  onValueChange?: (value: string) => void;
  /** Form field name; a hidden native input carries the value when the select sits in a `<form>`. */
  name?: string;
  /** id of the trigger, so a visible `<label htmlFor>` reaches it. */
  id?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean | "true" | "false";
  disabled?: boolean;
  required?: boolean;
  /** Trigger text when `value` matches no option. */
  placeholder?: string;
  /** Content before the value inside the trigger (an icon, a prefix). */
  leading?: React.ReactNode;
  /** Same heights as Button. */
  size?: keyof typeof triggerSizes;
  className?: string;
  contentClassName?: string;
  "data-testid"?: string;
  onBlur?: React.FocusEventHandler<HTMLButtonElement>;
  onOpenChange?: (open: boolean) => void;
}

/**
 * Themed replacement for a native `<select>`. The open list is drawn by Radix in a portal on
 * `surface-raised`, so it follows the app theme instead of the OS appearance. Arrow keys, type-ahead,
 * Home/End and Escape work; focus returns to the trigger.
 */
export const Select = React.forwardRef<HTMLButtonElement, SelectProps>(function Select(
  {
    children,
    value,
    defaultValue,
    onValueChange,
    name,
    id,
    disabled,
    required,
    placeholder,
    leading,
    size = "md",
    className,
    contentClassName,
    "data-testid": testId,
    onBlur,
    onOpenChange,
    ...aria
  },
  ref,
) {
  const options = readOptions(children);
  // Inside a <form> Radix keeps a hidden native <select> in step with `value` and reports its `change`
  // back; while the matching <option> is not registered yet (a value set before its options load, or in
  // the same render) that report is "" and would wipe the caller's state. Only a change the user caused
  // is passed on. The flag is set by handlers on the Item / Trigger themselves (Radix composes the
  // caller's handler before its own inside one listener, so the flag is up exactly when Radix calls
  // onValueChange synchronously) and cleared right after. Do NOT set it from a capture listener on
  // Content: a real browser runs a microtask checkpoint after every listener, so a flag cleared by a
  // microtask is already down when Radix's bubble-phase pointerup handler selects the item (jsdom
  // dispatches synchronously and hides this).
  const userAction = React.useRef(false);
  const markUserAction = () => {
    userAction.current = true;
    queueMicrotask(() => {
      userAction.current = false;
    });
  };
  const selected = value === undefined ? undefined : options.find((option) => option.value === value);
  return (
    <SelectPrimitive.Root
      value={value === undefined ? undefined : toInternal(value)}
      defaultValue={defaultValue === undefined ? undefined : toInternal(defaultValue)}
      onValueChange={
        onValueChange
          ? (next) => {
              if (userAction.current) onValueChange(fromInternal(next));
            }
          : undefined
      }
      onOpenChange={onOpenChange}
      name={name}
      disabled={disabled}
      required={required}
    >
      <SelectPrimitive.Trigger
        ref={ref}
        id={id}
        data-testid={testId}
        value={value}
        data-value={value}
        data-select-trigger=""
        data-options={JSON.stringify(options.map((option) => ({ value: option.value, label: textOf(option.label), disabled: option.disabled })))}
        onBlur={onBlur}
        onKeyDown={markUserAction}
        className={cn(
          "inline-flex w-full min-w-0 items-center justify-between gap-2 rounded-md border border-input bg-surface text-left text-foreground transition-colors hover:bg-fill-hover motion-reduce:transition-none",
          "disabled:pointer-events-none disabled:opacity-50 data-[placeholder]:text-muted-foreground aria-[invalid=true]:border-destructive",
          focusRing,
          triggerSizes[size],
          className,
        )}
        {...aria}
      >
        {leading ? <span className="flex shrink-0 items-center gap-1.5 text-muted-foreground">{leading}</span> : null}
        <span className="min-w-0 flex-1 truncate">
          <SelectPrimitive.Value placeholder={placeholder}>{selected ? selected.label : value ? placeholder : undefined}</SelectPrimitive.Value>
        </span>
        <SelectPrimitive.Icon className="shrink-0 text-muted-foreground">
          <ChevronDown className="size-icon" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          sideOffset={6}
          collisionPadding={8}
          className={cn(
            "z-50 max-h-[var(--radix-select-content-available-height)] min-w-[var(--radix-select-trigger-width)] max-w-[var(--radix-select-content-available-width)] overflow-hidden rounded-lg border border-border bg-surface-raised text-label text-foreground shadow-raised",
            contentClassName,
          )}
        >
          <SelectPrimitive.Viewport className="max-h-[min(24rem,var(--radix-select-content-available-height))] overflow-y-auto p-1">
            {options.map((option) => (
              <SelectPrimitive.Item
                key={option.value}
                value={toInternal(option.value)}
                data-value={option.value}
                disabled={option.disabled}
                onPointerUp={markUserAction}
                onClick={markUserAction}
                onKeyDown={markUserAction}
                className="relative flex min-h-7 cursor-default select-none items-center gap-2 rounded-sm py-1 pl-7 pr-2 outline-none data-[disabled]:pointer-events-none data-[disabled]:opacity-50 data-[highlighted]:bg-fill-active"
              >
                <span className="absolute left-2 flex size-icon items-center justify-center">
                  <SelectPrimitive.ItemIndicator>
                    <Check className="size-icon" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                  </SelectPrimitive.ItemIndicator>
                </span>
                <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
});
