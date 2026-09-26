"use client";

import { LibraryIndex } from "@/components/layout/library-index";
import { useReports } from "@/lib/api/queries";

export default function ReportsPage() {
  const { data, isPending, isError } = useReports();

  return (
    <LibraryIndex
      title="Reports"
      noun="reports"
      newHref="/reports/new"
      newLabel="New report"
      emptyHeadline="No reports yet"
      emptyBody="Ask your warehouse a question and DataTalk writes up the answer, with every number citing the query behind it."
      isPending={isPending}
      isError={isError}
      items={(data ?? []).map((r) => ({
        id: r.id,
        href: `/reports/${r.id}`,
        title: r.request,
        createdAt: r.created_at,
      }))}
    />
  );
}
