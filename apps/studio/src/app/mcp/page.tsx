import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function MCPPage() {
  return (
    <Layout>
      <EmptyState
        title="MCP Servers"
        description="Manage Model Context Protocol servers. Coming in a later milestone."
      />
    </Layout>
  );
}
