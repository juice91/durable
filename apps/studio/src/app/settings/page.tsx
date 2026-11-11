import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";

export default function SettingsPage() {
  return (
    <Layout>
      <EmptyState
        title="Settings"
        description="Configure application settings. Coming in a later milestone."
      />
    </Layout>
  );
}
