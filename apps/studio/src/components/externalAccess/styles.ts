// The form and notice styles the External access page's sections share.
export const fieldClass =
  "w-full min-w-0 rounded-md border border-input bg-surface px-3 py-1.5 text-label text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
export const labelClass = "block text-label font-medium text-foreground";
export const hintClass = "text-caption text-muted-foreground";
export const alertClass = "rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text";

export function message(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}
