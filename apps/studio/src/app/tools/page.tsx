import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function ToolsPage() {
  return (
    <Layout>
      <EmptyState
        title="Tools"
        description="Manage tools for AI agents. Coming in a later milestone."
      />
    </Layout>
  );
}
