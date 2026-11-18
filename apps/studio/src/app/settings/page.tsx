import { Suspense } from "react";
import { PageHeader } from "@/components/frame/PageHeader";
import { SettingsSections } from "@/components/settings/SettingsSections";
import { navCrumb } from "@/lib/nav";

export default function SettingsPage() {
  return (
    <>
      <PageHeader crumbs={[navCrumb("settings", { current: true })]} />
      <Suspense fallback={null}>
        <SettingsSections />
      </Suspense>
    </>
  );
}
