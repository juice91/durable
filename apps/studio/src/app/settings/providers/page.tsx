import { redirect } from "next/navigation";
import { routes } from "@/lib/routes";

/** Providers is a tab of Settings now (the ST.1 row of `lib/routes/legacy.ts`); the URL stays for bookmarks. */
export default function ProvidersPage() {
  redirect(routes.settingsProviders());
}
