import { EmptyState } from "@/components/shared/EmptyState";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";

export default function SettingsPage() {
  return (
    <>
      <PageHeader crumbs={[navCrumb("settings", { current: true })]} />
      <div className="px-4 py-6">
        <EmptyState
          title="Nothing to configure yet"
          description="Configure application settings. Coming in a later milestone."
        />
      </div>
    </>
  );
}
