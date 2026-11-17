import React from "react";
import { Button } from "@/components/ui/Button";
import { alertClass } from "@/components/evals/evalsUi";
import type { PagedList } from "./usePagedList";

/** The "Load more" row under a paged list, with the error of a failed further page. */
export function LoadMore({ list, label }: { list: Pick<PagedList<unknown>, "hasMore" | "loadingMore" | "moreError" | "loadMore">; label: string }) {
  if (!list.hasMore && !list.moreError) return null;
  return (
    <div className="flex min-w-0 flex-col items-center gap-2" data-testid="eval-overview-more">
      {list.moreError ? <p role="alert" className={alertClass}>{list.moreError}</p> : null}
      {list.hasMore ? (
        <Button onClick={list.loadMore} disabled={list.loadingMore} aria-label={`Load more ${label}`}>
          {list.loadingMore ? "Loading…" : "Load more"}
        </Button>
      ) : null}
    </div>
  );
}
