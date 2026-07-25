"use client";

import type { CapturedQuery } from "@/lib/api/types";
import { QueryCard } from "@/components/run/query-card";
import { useSources } from "./sources-context";

/**
 * The chain of custody for a document: every dataset behind every figure.
 *
 * The old UI held all of this and showed none of it -- `queries[]` was fetched
 * and dropped. Surfacing it is what lets an analyst vouch for a number.
 */
export function SourcesRail({ queries }: { queries: CapturedQuery[] }) {
  const sources = useSources();

  if (queries.length === 0) return null;

  return (
    <aside className="space-y-2" aria-labelledby="sources-heading">
      <h2 id="sources-heading" className="label-caps text-ink-tertiary">
        Sources · {queries.length}
      </h2>
      <div className="rounded-[6px] border border-border bg-card">
        {queries.map((query) => (
          <QueryCard
            key={query.dataset_id}
            highlighted={sources?.active === query.dataset_id}
            onHover={(id) => sources?.hover(id)}
            step={{
              datasetId: query.dataset_id,
              sql: query.sql,
              rowCount: query.row_count,
              columns: query.columns,
              retries: [],
              state: "done",
            }}
          />
        ))}
      </div>
    </aside>
  );
}
