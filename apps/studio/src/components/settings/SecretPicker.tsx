"use client";

import React, { useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import { putSecret } from "@/lib/secrets";
import { Select } from "@/components/ui/Select";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
export const NEW_SECRET = "__new__";

/**
 * Chooses a stored secret by NAME (the caller sends `secret:<name>`). "Create new secret…" opens a
 * masked inline form that stores the value, then selects the new name. The value is never kept past
 * the request and never shown.
 */
export function SecretPicker({ label, names, value, onChange, onCreated }: { label: string; names: string[]; value: string; onChange: (name: string) => void; onCreated: (name: string) => void }) {
  const id = useId();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const trimmed = name.trim();
    if (busy || !trimmed || !secret) return;
    setBusy(true);
    setError(null);
    try {
      await putSecret(trimmed, secret);
      setSecret("");
      setName("");
      setCreating(false);
      onCreated(trimmed);
      onChange(trimmed);
    } catch (reason: unknown) {
      // Built from the response, never from what was typed.
      setError(reason instanceof Error && reason.message ? reason.message : "The secret could not be saved");
    } finally {
      setBusy(false);
    }
  };

  const options = value && !names.includes(value) ? [value, ...names] : names;
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="space-y-1">
        <label htmlFor={`${id}-select`} className="block text-sm font-medium text-foreground">
          {label}
        </label>
        <Select
          id={`${id}-select`}
          value={creating ? NEW_SECRET : value}
          onValueChange={(v) => {
            if (v === NEW_SECRET) {
              setCreating(true);
            } else {
              setCreating(false);
              onChange(v);
            }
          }}
          className={`${inputClass} font-mono`}
        >
          <option value="">Choose a stored secret…</option>
          {options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
          <option value={NEW_SECRET}>Create new secret…</option>
        </Select>
      </div>
      {creating ? (
        <div className="flex min-w-0 flex-col gap-2 rounded-md border border-border p-3">
          <div className="space-y-1">
            <label htmlFor={`${id}-name`} className="block text-sm font-medium text-foreground">
              New secret name
            </label>
            <input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" spellCheck={false} placeholder="my-service-client-id" className={`${inputClass} font-mono`} />
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-value`} className="block text-sm font-medium text-foreground">
              New secret value
            </label>
            <input id={`${id}-value`} type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="off" spellCheck={false} className={`${inputClass} font-mono`} />
          </div>
          {error ? (
            <p role="alert" className="text-label text-destructive-text">
              {error}
            </p>
          ) : null}
          <div className="flex gap-2">
            <Button type="button" variant="primary" size="sm" disabled={busy || !name.trim() || !secret} onClick={() => void save()}>
              Save secret
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => {
                setCreating(false);
                setSecret("");
                setError(null);
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
