"use client";

import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { Composer } from "@/components/run/composer";
import { GenerationProgress } from "@/components/run/generation-progress";
import { DocumentView } from "@/components/doc/block-renderer";
import { InsightPanel } from "@/components/doc/insight-panel";
import { SourcesProvider } from "@/components/doc/sources-context";
import { SourcesRail } from "@/components/doc/sources-rail";
import { TemplateGallery } from "@/components/dashboard/template-gallery";
import { useNdjsonRun } from "@/hooks/use-ndjson-run";
import { getDashboard, streamDashboard } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queries";
import { Button } from "@/components/ui/button";

const EXAMPLES = [
  "An operations dashboard for support: volume, SLA, and the accounts at risk",
  "Revenue KPIs with month-over-month movement",
];

export default function NewDashboardPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const run = useNdjsonRun();
  const { state } = run;
  const idle = state.phase === "idle";

  const generate = (request: string, useMemory: boolean) =>
    run.start(
      request,
      (signal) => streamDashboard(request, useMemory, signal),
      async (id, signal) => {
        qc.invalidateQueries({ queryKey: qk.dashboards });
        if (id === null) return;
        // Hand off to the saved dashboard -- filters, refresh and analysis live
        // there. Load it first so the grid never blanks to a skeleton.
        await qc.prefetchQuery({
          queryKey: qk.dashboard(id),
          queryFn: () => getDashboard(id),
        });
        if (!signal.aborted) router.replace(`/dashboards/${id}`);
      },
    );

  return (
    <>
      <PageHeader
        title={idle ? "New dashboard" : state.request}
        meta={idle ? undefined : `${state.steps.length} queries`}
        actions={
          !idle ? (
            <Button variant="outline" size="sm" onClick={run.reset}>
              New dashboard
            </Button>
          ) : undefined
        }
      />

      <PageBody className="space-y-10">
        {idle && (
          <>
            <div className="py-8 sm:py-14">
              <Composer
                heading="Build a dashboard"
                subheading="one request, a whole grid."
                placeholder="e.g. An operations dashboard for support: volume, SLA compliance, and the accounts at risk."
                examples={EXAMPLES}
                submitLabel="Generate"
                onSubmit={generate}
              />
            </div>
            <TemplateGallery />
          </>
        )}

        {!idle && !state.document && (
          <>
            <GenerationProgress
              state={state}
              onStop={run.stop}
              onRefine={run.reset}
            />
            {/* The findings land right before the author call — the longest
                silent stretch — so they double as real progress content. */}
            {state.insights && <InsightPanel insights={state.insights} />}
          </>
        )}

        {state.document && (
          <SourcesProvider queries={state.queries}>
            {/* Dashboards ignore the 68ch measure: every block is full width. */}
            <DocumentView document={state.document} measure={false} />
            {state.insights && (
              <InsightPanel
                insights={state.insights}
                className="border-t border-border pt-8"
              />
            )}
            {state.savedId !== null && (
              <p className="text-[13px] text-ink-tertiary" aria-live="polite">
                Saved as dashboard #{state.savedId} &middot; opening the live
                view&hellip;
              </p>
            )}
            <SourcesRail queries={state.queries} />
          </SourcesProvider>
        )}
      </PageBody>
    </>
  );
}
