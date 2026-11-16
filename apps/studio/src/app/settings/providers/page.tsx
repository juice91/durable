import { PageHeader } from "@/components/frame/PageHeader";
import { ProvidersSettings } from "@/components/settings/ProvidersSettings";
import { navCrumb } from "@/lib/nav";

export default function ProvidersPage() {
  return (
    <>
      <PageHeader crumbs={[navCrumb("settings"), { label: "Providers" }]} />
      <ProvidersSettings />
    </>
  );
}
