import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function TracesPage() {
  return (
    <Layout>
      <EmptyState
        title="Traces"
        description="View distributed traces. Coming in a later milestone."
      />
    </Layout>
  );
}
