import { Suspense } from "react";
import { ThreadsPage } from "@/components/threads/ThreadsPage";

export default function ThreadsRoute() {
  return (
    <Suspense fallback={null}>
      <ThreadsPage />
    </Suspense>
  );
}
