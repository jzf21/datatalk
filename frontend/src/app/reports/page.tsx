"use client";

import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { Composer } from "@/components/run/composer";
import { GenerationProgress } from "@/components/run/generation-progress";
import { DocumentView } from "@/components/doc/block-renderer";
import { SourcesProvider } from "@/components/doc/sources-context";
import { SourcesRail } from "@/components/doc/sources-rail";
import { useNdjsonRun, type RunState } from "@/hooks/use-ndjson-run";
import { streamReport } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queries";
import { Button } from "@/components/ui/button";

const EXAMPLES = [
  "Summarize ticket resolution trends by account over the last 6 months",
  "Which accounts breached SLA most often, and by how much?",
  "Show month-over-month volume and flag anything anomalous",
];

export default function ReportsPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const run = useNdjsonRun();
  const { state } = run;

  const generate = (request: string, useMemory: boolean) =>
    run.start(
      request,
      (signal) => streamReport(request, useMemory, signal),
      (id) => {
        // The list is stale the moment a run persists.
        qc.invalidateQueries({ queryKey: qk.reports });
        if (id !== null) router.prefetch(`/reports/${id}`);
      },
    );

  const idle = state.phase === "idle";

  return (
    <>
      <PageHeader
        title={idle ? "New report" : state.request}
        meta={idle ? undefined : summarize(state)}
        actions={
          !idle ? (
            <Button variant="outline" size="sm" onClick={run.reset}>
              New report
            </Button>
          ) : undefined
        }
      />

      <PageBody className="space-y-10">
        {idle && (
          <div className="py-8 sm:py-14">
            <Composer
              heading="Ask your warehouse"
              subheading="a question worth an answer."
              placeholder="e.g. Summarize ticket resolution trends by account, and call out anything unusual."
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
            <div className="grid gap-10 xl:grid-cols-[minmax(0,1fr)_320px]">
              <DocumentView document={state.document} />
              <SourcesRail queries={state.queries} />
            </div>

            {state.savedId !== null && (
              <p className="text-[13px] text-ink-secondary">
                Saved ·{" "}
                <a
                  href={`/reports/${state.savedId}`}
                  className="text-link underline underline-offset-2"
                >
                  permalink to report #{state.savedId}
                </a>{" "}
                — open it to ask follow-up questions.
              </p>
            )}
          </SourcesProvider>
        )}
      </PageBody>
    </>
  );
}

function summarize(state: RunState): string {
  const rows = state.queries.reduce((sum, q) => sum + q.row_count, 0);
  const parts = [
    state.plan.length > 0 && `${state.plan.length} sections`,
    state.steps.length > 0 && `${state.steps.length} queries`,
    rows > 0 && `${rows.toLocaleString()} rows`,
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : "Working…";
}
