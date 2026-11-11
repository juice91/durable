import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";
import { navSections, allNavItems } from "@/lib/nav";

const pages = allNavItems.map((item) => item.href);

export default function DashboardPage() {
  return (
    <Layout>
      <EmptyState
        title="Dashboard"
        description="Welcome to the AI Platform Studio. This page will be populated in a later milestone."
      />
    </Layout>
  );
}
