"use client";

import React from "react";
import { SlidersHorizontal } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { EMPTY_MODEL_SETTINGS_TEXT, MODEL_SETTING_FIELDS, settingsProblems, type ModelSettingKey, type ModelSettingsText } from "@/lib/modelSettings";
import { Button, focusRing } from "@/components/ui/Button";
import { useMediaQuery } from "@/components/frame/useMediaQuery";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/Popover";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { ModelSettingsFields } from "@/components/shared/ModelSettingsFields";

/** Below 640 px the popover is a Sheet (layout rule 7). */
export const MODEL_SETTINGS_SHEET_QUERY = "(max-width: 639.98px)";
export const MODEL_SETTINGS_PANEL_ID = "chat-model-settings-panel";
export const MODEL_SETTINGS_TITLE = "Model settings";
const ALL_KEYS = MODEL_SETTING_FIELDS.map((field) => field.key);

export interface ModelSettingsPopoverProps {
  /** The typed values for this chat: "" leaves a setting to the agent version. */
  value: ModelSettingsText;
  onChange: (value: ModelSettingsText) => void;
  /** Messages from a refused turn start, by field ("" for the settings as a whole), shown until the value changes. */
  turnErrors?: Record<string, string> | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  disabled?: boolean;
}

/** How many settings are set. */
export function setCount(value: ModelSettingsText): number {
  return ALL_KEYS.filter((key) => value[key].trim() !== "").length;
}

/** The button's accessible summary: "none set", "2 set", "… (problems)". */
export function modelSettingsSummary(count: number, hasProblems: boolean): string {
  const base = count === 0 ? "using the agent's values" : `${count} set for this chat`;
  return hasProblems ? `${base} (problems)` : base;
}

/** Whether the typed values hold a problem (an out-of-range number): the chat must not send. */
export function modelSettingsInvalid(value: ModelSettingsText): boolean {
  return Object.keys(settingsProblems(value)).length > 0;
}

const SettingsButton = React.forwardRef<HTMLButtonElement, React.ButtonHTMLAttributes<HTMLButtonElement> & { summary: string; badge: string | null; invalid: boolean }>(
  function SettingsButton({ summary, badge, invalid, className, ...props }, ref) {
    return (
      <button
        ref={ref}
        type="button"
        id="chat-model-settings-toggle"
        aria-label={`${MODEL_SETTINGS_TITLE}: ${summary}`}
        data-testid="composer-model-settings-button"
        className={cn(
          "relative inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-fill-hover hover:text-foreground data-[state=open]:bg-fill-selected data-[state=open]:text-foreground disabled:pointer-events-none disabled:opacity-50",
          invalid && "text-destructive-text",
          focusRing,
          className,
        )}
        {...props}
      >
        <SlidersHorizontal aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-icon" />
        {badge ? (
          <span
            aria-hidden="true"
            data-testid="composer-model-settings-badge"
            className={cn(
              "absolute -right-0.5 -top-0.5 min-w-4 rounded-full px-1 text-center text-badge font-semibold leading-4",
              invalid ? "bg-destructive text-destructive-foreground" : "bg-primary text-primary-foreground",
            )}
          >
            {badge}
          </span>
        ) : null}
      </button>
    );
  },
);

/**
 * The composer's model settings control: a button (badge: how many are set, `!` on a problem) that
 * opens a popover of the settings this chat's runs override (temperature, max tokens, top P, top K,
 * penalties, seed, max steps, max retries). Empty means unset: the agent version's value applies.
 * Out-of-range values show an inline message and stop the send. The button is a native button in
 * the tab order; Enter or Space opens the panel, focus moves into it, Escape closes it and returns
 * focus to the button.
 */
export function ModelSettingsPopover({ value, onChange, turnErrors = null, open, onOpenChange, disabled = false }: ModelSettingsPopoverProps) {
  const count = setCount(value);
  const problems = settingsProblems(value);
  const serverProblems = turnErrors ? (Object.fromEntries(Object.entries(turnErrors).filter(([key]) => key !== "")) as Partial<Record<ModelSettingKey, string>>) : undefined;
  const general = turnErrors?.[""] ?? null;
  const invalid = Object.keys(problems).length > 0 || Boolean(turnErrors && Object.keys(turnErrors).length > 0);
  const summary = modelSettingsSummary(count, invalid);
  const badge = invalid ? "!" : count > 0 ? String(count) : null;

  const sheet = useMediaQuery(MODEL_SETTINGS_SHEET_QUERY, false);
  const buttonProps = { summary, badge, invalid, disabled, "aria-controls": MODEL_SETTINGS_PANEL_ID };
  const panel = (
    <section aria-label={`${MODEL_SETTINGS_TITLE} values`} className="min-h-0 space-y-4">
      <p className="text-label text-muted-foreground">
        Sent with every message in this chat. Leave a field empty to use the agent version&apos;s value.
      </p>
      {general ? (
        <p role="alert" data-testid="model-settings-turn-error" className="text-label text-destructive-text">
          {general}
        </p>
      ) : null}
      <ModelSettingsFields
        keys={ALL_KEYS}
        values={value}
        idFor={(key) => `chat-setting-${key}`}
        onChange={(key, next) => onChange({ ...value, [key]: next })}
        serverProblems={serverProblems}
      />
      <div className="flex items-center gap-3 border-t border-border pt-3">
        <Button size="sm" variant="ghost" onClick={() => onChange({ ...EMPTY_MODEL_SETTINGS_TEXT })} disabled={count === 0}>
          Clear
        </Button>
      </div>
    </section>
  );

  if (sheet) {
    return (
      <>
        <SettingsButton {...buttonProps} aria-haspopup="dialog" aria-expanded={open} data-state={open ? "open" : "closed"} onClick={() => onOpenChange(!open)} />
        <Sheet open={open} onOpenChange={onOpenChange}>
          <SheetContent id={MODEL_SETTINGS_PANEL_ID} size="md" aria-describedby={undefined} data-testid="model-settings-sheet">
            <SheetHeader title={MODEL_SETTINGS_TITLE} />
            <SheetBody>{panel}</SheetBody>
          </SheetContent>
        </Sheet>
      </>
    );
  }

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <SettingsButton {...buttonProps} />
      </PopoverTrigger>
      <PopoverContent
        id={MODEL_SETTINGS_PANEL_ID}
        aria-label={MODEL_SETTINGS_TITLE}
        data-testid="model-settings-popover"
        side="top"
        align="start"
        className="flex max-h-[var(--radix-popover-content-available-height)] w-80 flex-col overflow-y-auto p-0 md:w-[480px]"
      >
        <div className="flex min-h-header shrink-0 items-center border-b border-border px-4 py-2">
          <h2 className="text-title font-semibold text-foreground">{MODEL_SETTINGS_TITLE}</h2>
        </div>
        <div className="min-h-0 p-4">{panel}</div>
      </PopoverContent>
    </Popover>
  );
}
