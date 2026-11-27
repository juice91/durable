import React from "react";

/** The API's rule (the server decides; this only saves a round trip). */
export const MIN_PASSWORD_LENGTH = 12;

const inputClass =
  "h-9 w-full rounded-md border border-input bg-background px-3 text-body text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function Field({ id, label, ...input }: { id: string; label: string } & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-label text-muted-foreground">{label}</label>
      <input id={id} name={id} required autoCapitalize="none" spellCheck={false} className={inputClass} {...input} />
    </div>
  );
}
