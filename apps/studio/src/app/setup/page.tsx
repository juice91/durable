"use client";

// The first-run link (`/setup#code=…`). The AuthGate in the root layout shows the setup page here while there is no admin;
// once the app is rendered there is nothing to set up, so this goes home.
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { routes } from "@/lib/routes";

export default function SetupPage() {
  const router = useRouter();
  useEffect(() => router.replace(routes.agents()), [router]);
  return null;
}
