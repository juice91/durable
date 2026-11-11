export const AVAILABLE_TOOLS = [
  "clock.now",
  "tickets.create",
] as const;

export type ToolName = (typeof AVAILABLE_TOOLS)[number];
