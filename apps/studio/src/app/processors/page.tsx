import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function ProcessorsPage() {
  return (
    <Layout>
      <EmptyState
        title="Processors"
        description="Manage data processors. Coming in a later milestone."
      />
    </Layout>
  );
}
