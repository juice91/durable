import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function RequestContextPage() {
  return (
    <Layout>
      <EmptyState
        title="Request Context"
        description="Manage request context for agents. Coming in a later milestone."
      />
    </Layout>
  );
}
