"use client";

import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { Composer } from "@/components/run/composer";
import { GenerationProgress } from "@/components/run/generation-progress";
import { DocumentView } from "@/components/doc/block-renderer";
import { SourcesProvider } from "@/components/doc/sources-context";
import { SourcesRail } from "@/components/doc/sources-rail";
import { useNdjsonRun } from "@/hooks/use-ndjson-run";
import { streamDashboard } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queries";
import { Button } from "@/components/ui/button";

const EXAMPLES = [
  "An operations dashboard for support: volume, SLA, and the accounts at risk",
  "Revenue KPIs with month-over-month movement",
];

export default function DashboardsPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const run = useNdjsonRun();
  const { state } = run;
  const idle = state.phase === "idle";

  const generate = (request: string, useMemory: boolean) =>
    run.start(
      request,
      (signal) => streamDashboard(request, useMemory, signal),
      (id) => {
        qc.invalidateQueries({ queryKey: qk.dashboards });
        if (id !== null) router.prefetch(`/dashboards/${id}`);
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
        )}

        {!idle && !state.document && (
          <GenerationProgress
            state={state}
            onStop={run.stop}
            onRefine={() => {
              run.stop();
              run.reset();
            }}
          />
        )}

        {state.document && (
          <SourcesProvider queries={state.queries}>
            {/* Dashboards ignore the 68ch measure: every block is full width. */}
            <DocumentView document={state.document} measure={false} />
            {state.savedId !== null && (
              <p className="text-[13px] text-ink-secondary">
                Saved ·{" "}
                <a
                  href={`/dashboards/${state.savedId}`}
                  className="text-link underline underline-offset-2"
                >
                  permalink to dashboard #{state.savedId}
                </a>
              </p>
            )}
            <SourcesRail queries={state.queries} />
          </SourcesProvider>
        )}
      </PageBody>
    </>
  );
}
