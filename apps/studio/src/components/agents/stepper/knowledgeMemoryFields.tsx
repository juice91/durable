"use client";

/** Fields shared by the Knowledge and Memory steps (RM.17). Tokens only, no fixed widths. */
import React, { useEffect, useRef, useState } from "react";
import { typedNumber } from "../knowledgeMemory";
import { fieldClass, labelClass } from "./fieldStyles";

export const problemClass = "mt-1 text-caption text-destructive-text";
export const hintClass = "mt-1 text-caption text-muted-foreground";
export const groupClass = "min-w-0 space-y-3 rounded-md border border-border p-3";
export const legendClass = "px-1 text-label font-semibold text-foreground";

/**
 * True once the element is not inside a `hidden` step (every step stays mounted; a step loads its
 * lists the first time it is shown, so other steps and their tests never wait on them).
 */
export function useShownOnce(ref: React.RefObject<HTMLElement | null>, force = false): boolean {
  const [shown, setShown] = useState(force);
  // After every render on purpose: the step is hidden or shown by an ancestor's `hidden`, which no
  // prop of the step reflects. It sets state at most once (guarded by `shown`), so it cannot loop.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!shown && ref.current && !ref.current.closest("[hidden]")) setShown(true);
  });
  return shown || force;
}

/** The problem text under a field (polite, persistent; empty when fine). */
export function FieldProblem({ id, message }: { id: string; message: string | null | undefined }) {
  return (
    <p id={id} aria-live="polite" className={problemClass}>
      {message ?? ""}
    </p>
  );
}

function describedBy(...ids: (string | null | undefined | false)[]): string | undefined {
  const list = ids.filter(Boolean);
  return list.length > 0 ? list.join(" ") : undefined;
}

/** The value of a number field as shown: a number, the text as typed, or empty (the default applies). */
function shownNumber(value: unknown): string {
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  return "";
}

/** What a number field reports when the browser could not parse the text (`validity.badInput`). */
export const BAD_NUMBER_INPUT = "(not a number)";

/**
 * A number field. It reports the value to store: the number; `defaultValue` when cleared (the
 * API's default, so the saved draft equals the API's dump) or undefined for an optional field; the
 * text as typed when it is not a number. A cleared field stays empty (its placeholder names the
 * default) even though the default is stored.
 */
export function NumberField({
  id,
  label,
  value,
  onChange,
  min,
  max,
  step = 1,
  defaultValue,
  placeholder,
  hint,
  problem,
}: {
  id: string;
  label: string;
  value: unknown;
  onChange: (value: unknown) => void;
  min: number;
  max: number;
  step?: number;
  defaultValue?: number;
  placeholder?: string;
  hint?: string;
  problem: string | null | undefined;
}) {
  const [text, setText] = useState(() => shownNumber(value));
  const emitted = useRef<unknown>(value);
  useEffect(() => {
    if (value !== emitted.current) {
      emitted.current = value;
      setText(shownNumber(value));
    }
  }, [value]);

  const change = (event: React.ChangeEvent<HTMLInputElement>) => {
    const raw = event.target.value;
    // A browser that cannot parse the text reports "" with `badInput`: flag it, never store the default.
    const next = event.target.validity?.badInput ? BAD_NUMBER_INPUT : typedNumber(raw, defaultValue);
    setText(raw);
    emitted.current = next;
    onChange(next);
  };

  const problemId = `${id}-problem`;
  const hintId = hint ? `${id}-hint` : null;
  return (
    <div className="min-w-0">
      <label htmlFor={id} className={labelClass}>
        {label}
      </label>
      <input
        id={id}
        type="number"
        inputMode={step < 1 ? "decimal" : "numeric"}
        min={min}
        max={max}
        step={step}
        placeholder={placeholder}
        value={text}
        onChange={change}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy(hintId, problemId)}
        className={fieldClass}
      />
      {hint ? (
        <p id={hintId!} className={hintClass}>
          {hint}
        </p>
      ) : null}
      <FieldProblem id={problemId} message={problem} />
    </div>
  );
}

function jsonText(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

/**
 * A JSON editor (a monospace textarea). It keeps the text as typed and reports the parsed value:
 * undefined for empty text, the parsed JSON when it parses, else the text itself (which the step's
 * checks report as "not valid JSON"). A change of `value` from outside (a mode switch) resets the text.
 */
export function JsonTextarea({
  id,
  label,
  value,
  onChange,
  rows = 6,
  placeholder,
  hint,
  problem,
}: {
  id: string;
  label: string;
  value: unknown;
  onChange: (value: unknown) => void;
  rows?: number;
  placeholder?: string;
  hint?: React.ReactNode;
  problem: string | null | undefined;
}) {
  const [text, setText] = useState(() => jsonText(value));
  const emitted = useRef<unknown>(value);
  useEffect(() => {
    if (value !== emitted.current) {
      emitted.current = value;
      setText(jsonText(value));
    }
  }, [value]);

  const change = (next: string) => {
    setText(next);
    let parsed: unknown;
    if (next.trim() === "") parsed = undefined;
    else {
      try {
        parsed = JSON.parse(next);
      } catch {
        parsed = next;
      }
    }
    emitted.current = parsed;
    onChange(parsed);
  };

  const problemId = `${id}-problem`;
  const hintId = hint ? `${id}-hint` : null;
  return (
    <div className="min-w-0">
      <label htmlFor={id} className={labelClass}>
        {label}
      </label>
      <textarea
        id={id}
        rows={rows}
        spellCheck={false}
        placeholder={placeholder}
        value={text}
        onChange={(e) => change(e.target.value)}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy(hintId, problemId)}
        className={`${fieldClass} font-mono text-caption`}
      />
      {hint ? (
        <p id={hintId!} className={hintClass}>
          {hint}
        </p>
      ) : null}
      <FieldProblem id={problemId} message={problem} />
    </div>
  );
}

/** One allowlist entry: a concrete model, or a `provider:*` wildcard (any name of that provider). */
export interface AllowlistOption {
  id: string;
  provider: string;
  name: string;
  wildcard: boolean;
  label: string;
}

/**
 * A model picker that offers allowlisted models only: the concrete entries, plus "provider: any
 * model" for a wildcard entry with a name field. A current value outside the allowlist stays
 * selected and is marked, so it is never silently replaced.
 */
export function AllowlistSelect({
  id,
  label,
  options,
  loadError,
  provider,
  name,
  onChange,
  problem,
  hint,
}: {
  id: string;
  label: string;
  options: AllowlistOption[] | null;
  loadError: string | null;
  provider: string;
  name: string;
  onChange: (provider: string, name: string) => void;
  problem: string | null | undefined;
  hint?: string;
}) {
  const current = provider ? `${provider}:${name}` : "";
  const exact = options?.find((o) => !o.wildcard && o.id === current);
  const wildcard = !exact && provider ? options?.find((o) => o.wildcard && o.provider === provider) : undefined;
  const selected = exact ? exact.id : wildcard ? wildcard.id : current;
  const outside = Boolean(current) && options !== null && !exact && !wildcard;
  const problemId = `${id}-problem`;
  const hintId = hint ? `${id}-hint` : null;
  return (
    <div className="min-w-0">
      <label htmlFor={id} className={labelClass}>
        {label}
      </label>
      <select
        id={id}
        value={selected}
        onChange={(e) => {
          const option = options?.find((o) => o.id === e.target.value);
          if (option) onChange(option.provider, option.wildcard ? "" : option.name);
        }}
        aria-invalid={problem || outside ? true : undefined}
        aria-describedby={describedBy(hintId, problemId)}
        className={fieldClass}
      >
        <option value="" disabled>
          {options === null && !loadError ? "Loading models…" : "Select a model…"}
        </option>
        {current && !exact && !wildcard ? <option value={current}>{`${current} (not in the allowlist)`}</option> : null}
        {(options ?? []).map((option) => (
          <option key={option.id} value={option.id}>
            {option.label}
          </option>
        ))}
      </select>
      {wildcard ? (
        <input
          id={`${id}-name`}
          type="text"
          aria-label={`${label}: ${wildcard.provider} model name`}
          placeholder={`${wildcard.provider} model name`}
          value={name}
          onChange={(e) => onChange(wildcard.provider, e.target.value.trim())}
          aria-invalid={problem ? true : undefined}
          aria-describedby={problemId}
          className={`${fieldClass} mt-2`}
        />
      ) : null}
      {hint ? (
        <p id={hintId!} className={hintClass}>
          {hint}
        </p>
      ) : null}
      {loadError ? (
        <p role="alert" className={problemClass}>
          Could not load models: {loadError}
        </p>
      ) : null}
      <FieldProblem id={problemId} message={problem ?? (outside ? "Not in the platform allowlist: publishing will fail" : null)} />
    </div>
  );
}

/** A labelled checkbox that turns a feature on or off. */
export function FeatureToggle({
  id,
  label,
  description,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  description?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="flex min-w-0 items-start gap-2">
      <input
        id={id}
        type="checkbox"
        className="mt-0.5 size-4 shrink-0 accent-primary"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        aria-describedby={description ? `${id}-description` : undefined}
      />
      <div className="min-w-0">
        <label htmlFor={id} className="text-label font-medium text-foreground">
          {label}
        </label>
        {description ? (
          <p id={`${id}-description`} className="text-caption text-muted-foreground">
            {description}
          </p>
        ) : null}
      </div>
    </div>
  );
}

/** A small radio group (fieldset + legend). */
export function RadioGroup<T extends string>({
  name,
  firstId,
  legend,
  value,
  options,
  onChange,
}: {
  name: string;
  /** The first radio's element id (a focus target for the step's checks). */
  firstId?: string;
  legend: string;
  value: T;
  options: { value: T; label: string; description?: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <fieldset className="min-w-0">
      <legend className={labelClass}>{legend}</legend>
      <div className="flex min-w-0 flex-wrap gap-x-4 gap-y-1">
        {options.map((option, index) => (
          <label key={option.value} className="inline-flex min-w-0 items-start gap-2 text-label text-foreground">
            <input
              id={index === 0 ? firstId : undefined}
              type="radio"
              name={name}
              value={option.value}
              className="mt-0.5 size-4 shrink-0 accent-primary"
              checked={value === option.value}
              onChange={() => onChange(option.value)}
            />
            <span className="min-w-0">
              {option.label}
              {option.description ? <span className="block text-caption text-muted-foreground">{option.description}</span> : null}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
