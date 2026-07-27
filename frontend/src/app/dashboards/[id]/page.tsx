"use client";

import { use, useState } from "react";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { DocumentView } from "@/components/doc/block-renderer";
import { InsightPanel } from "@/components/doc/insight-panel";
import { SourcesProvider } from "@/components/doc/sources-context";
import { SourcesRail } from "@/components/doc/sources-rail";
import { Markdown } from "@/components/markdown/markdown";
import { useAnalyzeDashboard, useDashboard } from "@/lib/api/queries";
import { formatRelativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

export default function DashboardPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const dashboardId = Number(id);
  const { data, isPending, isError, error } = useDashboard(
    Number.isInteger(dashboardId) ? dashboardId : null,
  );
  const analyze = useAnalyzeDashboard(dashboardId);
  const [elapsed, setElapsed] = useState(0);

  if (isError) {
    return (
      <>
        <PageHeader title="Not found" />
        <PageBody>
          <p className="text-[13px] text-ink-secondary">{String(error)}</p>
        </PageBody>
      </>
    );
  }

  if (isPending || !data) {
    return (
      <>
        <PageHeader title="Loading…" />
        <PageBody className="space-y-4">
          <div className="doc-row">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} style={{ "--w": 3, "--w-md": 6 } as React.CSSProperties}>
                <Skeleton className="h-20 w-full" />
              </div>
            ))}
          </div>
          <Skeleton className="h-[220px] w-full" />
        </PageBody>
      </>
    );
  }

  const rows = data.queries.reduce((sum, q) => sum + q.row_count, 0);

  return (
    <>
      <PageHeader
        title={data.title || data.request}
        meta={`${data.queries.length} queries · ${rows.toLocaleString()} rows · ${formatRelativeTime(data.created_at)}`}
      />
      <PageBody className="space-y-10">
        <SourcesProvider queries={data.queries}>
          <DocumentView document={data.document} measure={false} />

          {data.insights && (
            <InsightPanel
              insights={data.insights}
              className="border-t border-border pt-8"
            />
          )}

          <section className="border-t border-border pt-8">
            <div className="mb-4 flex items-center justify-between gap-4">
              <h2 className="label-caps text-ink-tertiary">Analysis</h2>
              <Button
                variant="outline"
                size="sm"
                disabled={analyze.isPending}
                onClick={() => {
                  setElapsed(Date.now());
                  analyze.mutate(undefined);
                }}
              >
                {analyze.isPending
                  ? "Analyzing…"
                  : data.analysis
                    ? "Re-analyze"
                    : "Analyze"}
              </Button>
            </div>

            {analyze.isPending && (
              <div className="space-y-2" aria-live="polite">
                <p className="text-[12px] text-ink-tertiary">
                  This usually takes 15&ndash;30 seconds.
                </p>
                {[0, 1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-3 w-full" />
                ))}
              </div>
            )}

            {analyze.isError && (
              <p
                role="alert"
                className="border-l-2 border-destructive py-1 pl-3 text-[13px] text-ink-secondary"
              >
                {String(analyze.error)}
              </p>
            )}

            {!analyze.isPending &&
              (analyze.data?.analysis || data.analysis ? (
                <Markdown className="doc-measure">
                  {analyze.data?.analysis ?? data.analysis ?? ""}
                </Markdown>
              ) : (
                <p className="text-[13px] text-ink-secondary">
                  No analysis yet. It reads the numbers already on this page — it
                  runs no new queries.
                </p>
              ))}
          </section>

          <SourcesRail queries={data.queries} />
        </SourcesProvider>
      </PageBody>
    </>
  );
}
