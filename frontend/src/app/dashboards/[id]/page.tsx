"use client";

import { Suspense, use, useCallback, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { LayoutGrid, SlidersHorizontal } from "lucide-react";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { DocumentView } from "@/components/doc/block-renderer";
import { InsightPanel } from "@/components/doc/insight-panel";
import { SourcesProvider } from "@/components/doc/sources-context";
import { SourcesRail } from "@/components/doc/sources-rail";
import { RefreshControls } from "@/components/dashboard/refresh-controls";
import { RefreshStatus } from "@/components/dashboard/refresh-status";
import { Markdown } from "@/components/markdown/markdown";
import {
  useAnalyzeDashboard,
  useDashboard,
  useDashboardData,
} from "@/lib/api/queries";
import { FilterBar } from "@/components/dashboard/filter-bar";
import { FilterConfigDialog } from "@/components/dashboard/filter-config-dialog";
import { LayoutEditor } from "@/components/dashboard/layout-editor";
import {
  decodeFilters,
  encodeFilters,
  isDefault as filtersAreDefault,
} from "@/lib/dashboards/filters";
import type { FilterValue } from "@/lib/api/types";
import { useAutoRefresh, labelFor } from "@/hooks/use-auto-refresh";
import { useNow } from "@/hooks/use-now";
import { formatRelativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

function LoadingDashboard() {
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

export default function DashboardPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  // The Suspense boundary is required, not stylistic: DashboardInner calls
  // useSearchParams, and without a boundary above it `next build` fails with
  // "Missing Suspense boundary with useSearchParams". `next dev` will not say so.
  return (
    <Suspense fallback={<LoadingDashboard />}>
      <DashboardInner id={id} />
    </Suspense>
  );
}

function DashboardInner({ id }: { id: string }) {
  const dashboardId = Number(id);
  const validId = Number.isInteger(dashboardId) ? dashboardId : null;

  const { data, isPending, isError, error } = useDashboard(validId);
  const analyze = useAnalyzeDashboard(dashboardId);
  const [, setElapsed] = useState(0);
  const [configuring, setConfiguring] = useState(false);
  // Bumped to re-open the editor on the stored dashboard after a server-side
  // change (an added widget) that the editor's local model does not know about.
  const [editing, setEditing] = useState<number | null>(null);

  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const defs = useMemo(() => data?.filters?.filters ?? [], [data]);
  const filters = useMemo(
    () => decodeFilters(defs, searchParams),
    [defs, searchParams],
  );

  // The URL is the single source of truth for filter state, not a mirror of it.
  // A filtered dashboard is a view, and views get shared and bookmarked; it also
  // makes Back undo a filter change, and makes reload preserve one -- which is
  // the same reload this whole feature is about.
  const setFilter = useCallback(
    (id: string, value: FilterValue) => {
      const next = encodeFilters(defs, { ...filters, [id]: value });
      const query = next.toString();
      // push, not replace: one deliberate change is one history entry.
      router.push(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [defs, filters, pathname, router],
  );

  const resetFilters = useCallback(() => {
    router.push(pathname, { scroll: false });
  }, [pathname, router]);

  const { ms: intervalMs, setMs: setIntervalMs } = useAutoRefresh(validId);
  const live = useDashboardData(validId, filters, intervalMs, !!data);
  const now = useNow();

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

  if (isPending || !data) return <LoadingDashboard />;

  // The stored snapshot paints immediately and is swapped in place once the
  // live document lands -- no flash, no blank grid, and the meta line below
  // says which of the two is on screen rather than leaving it ambiguous.
  const fresh = live.data ?? null;
  // Partial failure degrades per widget -- that is the design. Total failure
  // does not: a warehouse that is down would otherwise replace every number on
  // the page with an italic note, which is strictly less useful than the last
  // numbers we know were real. Same reasoning that keeps a refresh from writing
  // the stored snapshot back. The banner below still says what happened.
  const allFailed =
    !!fresh && fresh.datasets.length > 0 && fresh.datasets.every((d) => !d.ok);
  const document = fresh && !allFailed ? fresh.document : data.document;
  const queries = data.queries;
  const isTemplate = "id" in (data.template ?? {});
  const rows = data.queries.reduce((sum, q) => sum + (q.row_count ?? 0), 0);
  const isStale = !fresh;

  const freshness =
    fresh && !allFailed
      ? `Updated ${formatRelativeTime(fresh.refreshed_at, now)}`
      : `Snapshot from ${formatRelativeTime(data.created_at, now)}`;
  const meta = [
    `${data.queries.length} queries`,
    `${rows.toLocaleString()} rows`,
    freshness,
    live.isFetching ? "refreshing…" : null,
    intervalMs ? `auto ${labelFor(intervalMs)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <>
      <PageHeader
        title={data.title || data.request}
        meta={meta}
        actions={
          <>
            {/* A template's filters come with its SQL; they are not
                reconfigured by the model-driven rewrite this dialog runs. */}
            {data.editable && editing === null && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setEditing(0)}
                title="Move, resize and restyle widgets"
              >
                <LayoutGrid className="size-3.5" />
                Edit layout
              </Button>
            )}
            {!isTemplate && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setConfiguring(true)}
                title="Choose which filters this dashboard has"
              >
                <SlidersHorizontal className="size-3.5" />
                Filters
              </Button>
            )}
            <RefreshControls
              intervalMs={intervalMs}
              onIntervalChange={setIntervalMs}
              onRefresh={() => live.refetch()}
              isFetching={live.isFetching}
            />
          </>
        }
      />

      {/* Mounted only while open, so its drafts start from the saved
          definitions each time rather than from a cancelled edit. */}
      {configuring && (
        <FilterConfigDialog
          dashboardId={dashboardId}
          open
          onOpenChange={setConfiguring}
          existing={defs}
          queries={data.queries}
        />
      )}
      <PageBody className="space-y-10">
        <SourcesProvider queries={queries}>
          <FilterBar
            defs={defs}
            values={filters}
            onChange={setFilter}
            onReset={resetFilters}
            isDefault={filtersAreDefault(defs, filters)}
            unfiltered={fresh?.unfiltered ?? []}
          />

          {live.isError && (
            <p
              role="alert"
              className="border-l-2 border-destructive py-1 pl-3 text-[13px] text-ink-secondary"
            >
              Could not refresh: {String(live.error)}. The numbers below are from{" "}
              {formatRelativeTime(data.created_at, now)}.
            </p>
          )}
          <RefreshStatus data={fresh} />

          {editing !== null ? (
            <LayoutEditor
              key={editing}
              dashboard={data}
              preview={document}
              onClose={() => setEditing(null)}
              onReload={() => setEditing((n) => (n ?? 0) + 1)}
            />
          ) : (
            <DocumentView document={document} measure={false} />
          )}

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

            {/*
              The analysis is prose about the numbers as they were when it ran.
              Once live data is on screen those numbers have moved, so saying so
              is not a nicety -- leaving it silent would break the same
              every-number-cites-its-query rule the rest of the product keeps.
            */}
            {!isStale && (data.analysis || analyze.data?.analysis) && (
              <p className="mb-3 text-[12px] text-ink-tertiary">
                Written against the numbers as of{" "}
                {formatRelativeTime(data.created_at, now)}. Re-analyze to read
                the current ones.
              </p>
            )}

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

          <SourcesRail queries={queries} />
        </SourcesProvider>
      </PageBody>
    </>
  );
}
