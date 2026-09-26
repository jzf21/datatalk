"use client";

import { LibraryIndex } from "@/components/layout/library-index";
import { useDashboards } from "@/lib/api/queries";

export default function DashboardsPage() {
  const { data, isPending, isError } = useDashboards();

  return (
    <LibraryIndex
      title="Dashboards"
      noun="dashboards"
      newHref="/dashboards/new"
      newLabel="New dashboard"
      emptyHeadline="No dashboards yet"
      emptyBody="Describe the dashboard you want, or start from a template. Dashboards re-run their queries, so the numbers stay live."
      isPending={isPending}
      isError={isError}
      items={(data ?? []).map((d) => ({
        id: d.id,
        href: `/dashboards/${d.id}`,
        title: d.title || d.request,
        createdAt: d.created_at,
      }))}
    />
  );
}
