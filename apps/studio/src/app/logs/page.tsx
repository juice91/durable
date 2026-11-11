import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function LogsPage() {
  return (
    <Layout>
      <EmptyState
        title="Logs"
        description="View application logs. Coming in a later milestone."
      />
    </Layout>
  );
}
