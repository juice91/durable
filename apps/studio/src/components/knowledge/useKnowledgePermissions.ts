import { usePermissions, type Permissions } from "@/lib/permissions";

/** One question for everything on a knowledge base's page: change it, search it, delete it. */
const ACTIONS = ["edit", "run", "archive"] as const;

export function useKnowledgePermissions(kbId: string | undefined): Permissions {
  return usePermissions("knowledge_base", kbId || undefined, ACTIONS);
}
