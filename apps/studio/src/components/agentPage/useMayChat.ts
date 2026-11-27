import type { Agent } from "@/lib/api";
import { usePermissions, type Permissions } from "@/lib/permissions";

/**
 * Whether the caller may chat with this agent. The built-in Architect answers everyone (the server limits a viewer to
 * its read-only tools), so it is never asked about; any other agent needs the server's `run` on it.
 */
export function useMayChat(agent: Pick<Agent, "id" | "builtin_role"> | null | undefined): Permissions {
  const architect = agent?.builtin_role === "architect";
  const permissions = usePermissions("agent", agent && !architect ? agent.id : undefined, ["run"]);
  return architect ? { loading: false, can: () => true } : permissions;
}
