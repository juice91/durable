// `<input type="datetime-local">` values <-> ISO timestamps. The input's value has no offset and is
// the viewer's local time; the API speaks UTC ISO (an ISO value without an offset is UTC there).

const LOCAL_INPUT_RE = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d)(?:\.(\d{1,3})\d*)?)?$/;

/** `datetime-local` value ("YYYY-MM-DDTHH:mm[:ss]", local time) -> UTC ISO; null when empty or invalid. */
export function localInputToIso(value: string): string | null {
  const m = LOCAL_INPUT_RE.exec(value.trim());
  if (!m) return null;
  const [year, month, day, hour, minute, second = "0", ms = "0"] = m.slice(1);
  const d = new Date(
    Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second), Number(ms.padEnd(3, "0")),
  );
  // Reject overflow such as 2026-02-31 (Date would roll it into March).
  if (Number.isNaN(d.getTime()) || d.getMonth() !== Number(month) - 1 || d.getDate() !== Number(day)) return null;
  return d.toISOString();
}

/** ISO (a value without an offset is UTC, as the API reads it) -> `datetime-local` value in local time. */
export function isoToLocalInput(value: string | null | undefined): string {
  if (!value) return "";
  const hasOffset = /([zZ]|[+-]\d\d:?\d\d)$/.test(value);
  const t = Date.parse(hasOffset || !value.includes("T") ? value : `${value}Z`);
  if (Number.isNaN(t)) return "";
  const d = new Date(t);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
