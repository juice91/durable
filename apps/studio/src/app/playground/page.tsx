import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function PlaygroundPage() {
  return (
    <Layout>
      <EmptyState
        title="Playground"
        description="Test and experiment with AI agents. Coming in a later milestone."
      />
    </Layout>
  );
}
