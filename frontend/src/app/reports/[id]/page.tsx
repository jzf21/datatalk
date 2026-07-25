"use client";

import { use } from "react";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { DocumentView } from "@/components/doc/block-renderer";
import { SourcesProvider } from "@/components/doc/sources-context";
import { SourcesRail } from "@/components/doc/sources-rail";
import { QAThread } from "@/components/qa/qa-thread";
import { useReport } from "@/lib/api/queries";
import { formatRelativeTime } from "@/lib/format";
import { Skeleton } from "@/components/ui/skeleton";

export default function ReportPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  // Next 16: route params arrive as a promise and are unwrapped with `use`.
  const { id } = use(params);
  const reportId = Number(id);
  const { data, isPending, isError, error } = useReport(
    Number.isInteger(reportId) ? reportId : null,
  );

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
          <Skeleton className="h-5 w-[60%]" />
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-[220px] w-full" />
        </PageBody>
      </>
    );
  }

  const rows = data.queries.reduce((sum, q) => sum + q.row_count, 0);

  return (
    <>
      <PageHeader
        title={data.request}
        meta={`${data.queries.length} queries · ${rows.toLocaleString()} rows · ${formatRelativeTime(data.created_at)}`}
      />
      <PageBody>
        <SourcesProvider queries={data.queries}>
          <div className="grid gap-10 xl:grid-cols-[minmax(0,1fr)_320px]">
            <div className="min-w-0">
              <DocumentView document={data.document} />
              <QAThread reportId={reportId} turns={data.qa_turns ?? []} />
            </div>
            <SourcesRail queries={data.queries} />
          </div>
        </SourcesProvider>
      </PageBody>
    </>
  );
}
