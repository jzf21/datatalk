"use client";

import { Info } from "lucide-react";

import type { DashboardData } from "@/lib/api/types";

/**
 * What a partial refresh has to admit.
 *
 * Not a toast: `providers.tsx` reserves those for transient confirmations, and
 * "two of your seven numbers are stale" is not transient. It sits above the grid
 * in the same Info-aside shape `block-renderer` already uses for a degraded
 * block, so the summary and the in-document notes read as one voice.
 */
export function RefreshStatus({ data }: { data: DashboardData | null }) {
  if (!data || !data.partial) return null;

  const failed = data.datasets.filter((d) => !d.ok);
  const parts: string[] = [];

  if (failed.length) {
    parts.push(
      `${failed.length} of ${data.datasets.length} ${
        data.datasets.length === 1 ? "query" : "queries"
      } could not be refreshed`,
    );
  }
  if (data.frozen_stats > 0) {
    parts.push(
      `${data.frozen_stats} stat ${
        data.frozen_stats === 1 ? "tile keeps its" : "tiles keep their"
      } original value`,
    );
  }
  if (!parts.length) return null;

  return (
    <aside
      role="status"
      className="flex items-start gap-2 border-l-2 border-destructive py-1 pl-3 text-[13px] text-ink-secondary"
    >
      <Info className="mt-0.5 size-3.5 shrink-0 text-ink-tertiary" />
      <div>
        <p>{parts.join(", and ")}.</p>
        {!data.exact && data.frozen_stats > 0 && (
          <p className="mt-1 text-[12px] text-ink-tertiary">
            This dashboard was created before live refresh existed, so the column
            behind each stat tile was never recorded. Regenerate it to make those
            tiles live too.
          </p>
        )}
        {failed.length > 0 && (
          <ul className="mt-1 space-y-0.5 text-[12px] text-ink-tertiary">
            {failed.map((d) => (
              <li key={d.dataset_id}>
                <span className="font-mono">{d.dataset_id}</span>
                {d.source ? ` · ${d.source}` : ""} — {d.message || d.reason}
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
