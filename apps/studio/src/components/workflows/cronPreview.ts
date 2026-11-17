const pad = (n: number) => String(n).padStart(2, "0");

/** A short reading of common 5-field cron patterns; the raw string when the pattern is not one of them. */
export function cronPreview(cron: string): string {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [minute, hour, day, month, weekday] = parts;
  if (day !== "*" || month !== "*" || weekday !== "*") return cron;
  const num = /^\d+$/;
  if (minute === "*" && hour === "*") return "Every minute";
  const step = /^\*\/(\d+)$/.exec(minute);
  if (step && hour === "*") return Number(step[1]) === 1 ? "Every minute" : `Every ${Number(step[1])} minutes`;
  if (num.test(minute) && Number(minute) < 60) {
    if (hour === "*") return `Every hour at :${pad(Number(minute))}`;
    if (num.test(hour) && Number(hour) < 24) return `Every day at ${pad(Number(hour))}:${pad(Number(minute))}`;
  }
  return cron;
}
