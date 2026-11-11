import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function WorkflowsPage() {
  return (
    <Layout>
      <EmptyState
        title="Workflows"
        description="Design and manage AI workflows. Coming in a later milestone."
      />
    </Layout>
  );
}
