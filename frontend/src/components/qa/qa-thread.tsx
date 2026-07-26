"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { CapturedQuery, QATurn } from "@/lib/api/types";
import { streamAsk } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queries";
import { useNdjsonRun } from "@/hooks/use-ndjson-run";
import { formatRelativeTime } from "@/lib/format";
import { DocumentView } from "@/components/doc/block-renderer";
import { QueryCard } from "@/components/run/query-card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";

/**
 * Not chat bubbles. An answer is a full block document that may contain a
 * 14-column table, and a bubble would cap its width and look broken.
 *
 * The question becomes marginalia -- ruled and indented; the answer sits at
 * full measure so its figures get the same bleed as the report's.
 */
export function QAThread({
  reportId,
  turns,
}: {
  reportId: number;
  turns: QATurn[];
}) {
  const qc = useQueryClient();
  const run = useNdjsonRun();
  const [question, setQuestion] = useState("");

  const ask = () => {
    const trimmed = question.trim();
    if (!trimmed || run.running) return;
    setQuestion("");
    run.start(
      trimmed,
      (signal) => streamAsk(reportId, trimmed, signal),
      () => qc.invalidateQueries({ queryKey: qk.report(reportId) }),
    );
  };

  return (
    <section className="mt-12 border-t border-border pt-8">
      <h2 className="label-caps mb-6 text-ink-tertiary">
        Follow-up{turns.length > 0 && ` · ${turns.length}`}
      </h2>

      <div className="space-y-10">
        {turns.map((turn) => (
          <Turn
            key={turn.id}
            question={turn.question}
            timestamp={turn.created_at}
            queries={turn.queries}
            document={turn.document}
          />
        ))}

        {run.state.phase !== "idle" && (
          <Turn
            question={run.state.request}
            queries={run.state.queries}
            document={run.state.document}
            pending={run.running}
            steps={run.state.steps}
            error={run.state.fatal}
          />
        )}
      </div>

      <div className="mt-8">
        <Label htmlFor="question" className="sr-only">
          Ask about this report
        </Label>
        <Textarea
          id="question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              ask();
            }
          }}
          placeholder="Ask about this report…"
          rows={2}
          className="resize-y bg-muted text-[15px]"
        />
        <div className="mt-2 flex items-center justify-between gap-4">
          <p className="text-[12px] text-ink-tertiary">
            Answers query your data sources fresh — they aren&rsquo;t limited to
            what&rsquo;s in this report.
          </p>
          <Button size="sm" onClick={ask} disabled={run.running || !question.trim()}>
            Ask
          </Button>
        </div>
      </div>
    </section>
  );
}

function Turn({
  question,
  timestamp,
  queries,
  document,
  pending,
  steps,
  error,
}: {
  question: string;
  timestamp?: string;
  queries: CapturedQuery[];
  document: { blocks: unknown[] } | null;
  pending?: boolean;
  steps?: { sql: string; retries: string[]; state: "running" | "done" }[];
  error?: string | null;
}) {
  const [showQueries, setShowQueries] = useState(false);

  return (
    <article className="space-y-4">
      <header className="flex items-start justify-between gap-4 border-l-2 border-border-strong pl-3">
        <p className="text-[15px] font-semibold text-ink-primary">{question}</p>
        {timestamp && (
          <time className="shrink-0 text-[12px] text-ink-tertiary">
            {formatRelativeTime(timestamp)}
          </time>
        )}
      </header>

      {queries.length > 0 && (
        <div className="pl-3">
          <button
            type="button"
            onClick={() => setShowQueries((v) => !v)}
            aria-expanded={showQueries}
            className="cite rounded-[2px] bg-muted px-1.5 py-0.5 text-ink-secondary hover:text-ink-primary"
          >
            {queries.length} {queries.length === 1 ? "query" : "queries"}{" "}
            {showQueries ? "▴" : "▾"}
          </button>
          {showQueries && (
            <div className="mt-2 rounded-[6px] border border-border bg-card">
              {queries.map((q) => (
                <QueryCard
                  key={q.dataset_id}
                  step={{
                    datasetId: q.dataset_id,
                    sql: q.sql,
                    rowCount: q.row_count,
                    columns: q.columns,
                    retries: [],
                    state: "done",
                  }}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Same grammar as the main run, compacted: no plan panel, since Q&A
          emits no plan event. */}
      {pending && !document && (
        <div className="space-y-2">
          {steps?.map((step, i) => (
            <QueryCard key={i} step={step} />
          ))}
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-[92%]" />
          <Skeleton className="h-3 w-[64%]" />
        </div>
      )}

      {error && (
        <p
          role="alert"
          className="border-l-2 border-[var(--status-warning)] py-1 pl-2 text-[13px] text-ink-secondary"
        >
          Answer failed — {error}
        </p>
      )}

      {document && (
        <DocumentView
          document={document as never}
          id={`turn-${question.slice(0, 8)}`}
        />
      )}
    </article>
  );
}
