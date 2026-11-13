"use client";

import { useParams } from "next/navigation";
import { EditorTab } from "@/components/agentPage/EditorTab";

/** `/agents/:id/editor`: the editor, versions and compare (SL.11 turns it into the stepped editor). */
export default function AgentEditorPage() {
  const params = useParams<{ id: string }>();
  return <EditorTab agentId={String(params.id)} />;
}
