import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function MetricsPage() {
  return (
    <Layout>
      <EmptyState
        title="Metrics"
        description="View system metrics. Coming in a later milestone."
      />
    </Layout>
  );
}
