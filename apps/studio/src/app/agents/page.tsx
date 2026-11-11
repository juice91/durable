import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function AgentsPage() {
  return (
    <Layout>
      <EmptyState
        title="Agents"
        description="Manage and create AI agents. Coming in a later milestone."
      />
    </Layout>
  );
}
