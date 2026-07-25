"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { Markdown } from "@/components/markdown/markdown";
import { useAnalyze, useReports } from "@/lib/api/queries";
import { formatRelativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export default function AnalyzePage() {
  return (
    <Suspense fallback={null}>
      <AnalyzeInner />
    </Suspense>
  );
}

function AnalyzeInner() {
  const params = useSearchParams();
  const deepLinked = params.get("report");

  const [source, setSource] = useState<"own" | "external">(
    deepLinked ? "own" : "own",
  );
  const [reportId, setReportId] = useState<number | null>(
    deepLinked ? Number(deepLinked) : null,
  );
  const [text, setText] = useState("");
  const [focus, setFocus] = useState("");

  const reports = useReports();
  const analyze = useAnalyze();

  const submit = () =>
    analyze.mutate(
      source === "own"
        ? { report_id: reportId ?? undefined, focus }
        : { text, focus },
    );

  const ready = source === "own" ? reportId !== null : text.trim().length > 0;

  return (
    <>
      <PageHeader
        title="Analyze"
        meta="Critique one of your reports, or paste text from elsewhere."
      />
      <PageBody className="space-y-6">
        <ToggleGroup
          type="single"
          value={source}
          onValueChange={(v) => v && setSource(v as "own" | "external")}
          aria-label="What to analyze"
        >
          <ToggleGroupItem value="own">One of my reports</ToggleGroupItem>
          <ToggleGroupItem value="external">Pasted text</ToggleGroupItem>
        </ToggleGroup>

        {source === "own" ? (
          <div>
            <Label className="label-caps text-ink-secondary">Report</Label>
            {reports.isPending ? (
              <Skeleton className="mt-1.5 h-9 w-full" />
            ) : reports.data?.length ? (
              <ul className="mt-1.5 max-h-64 overflow-y-auto rounded-[6px] border border-border bg-card">
                {reports.data.map((report) => (
                  <li key={report.id}>
                    <button
                      type="button"
                      onClick={() => setReportId(report.id)}
                      aria-pressed={reportId === report.id}
                      className={`flex w-full items-baseline gap-2 border-b border-border px-3 py-2 text-left text-[13px] last:border-b-0 hover:bg-accent/50 ${
                        reportId === report.id ? "bg-accent" : ""
                      }`}
                    >
                      <span className="cite shrink-0 text-ink-tertiary">
                        #{report.id}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-ink-primary">
                        {report.request}
                      </span>
                      <span className="shrink-0 text-[12px] text-ink-tertiary">
                        {formatRelativeTime(report.created_at)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1.5 text-[13px] text-ink-secondary">
                No reports yet — generate one first.
              </p>
            )}
          </div>
        ) : (
          <div>
            <Label htmlFor="external" className="label-caps text-ink-secondary">
              Text to analyze
            </Label>
            <Textarea
              id="external"
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={10}
              placeholder="Paste a report, a summary, or anything else you want critiqued."
              className="mt-1.5 resize-y bg-muted"
            />
          </div>
        )}

        <div>
          <Label htmlFor="focus" className="label-caps text-ink-secondary">
            Focus <span className="normal-case tracking-normal">(optional)</span>
          </Label>
          <Input
            id="focus"
            value={focus}
            onChange={(e) => setFocus(e.target.value)}
            placeholder="e.g. Are the SLA conclusions actually supported?"
            className="mt-1.5 bg-muted"
          />
        </div>

        <Button onClick={submit} disabled={!ready || analyze.isPending}>
          {analyze.isPending ? "Analyzing…" : "Analyze"}
        </Button>

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

        {analyze.data && (
          <section className="border-t border-border pt-6">
            <Markdown className="doc-measure">{analyze.data.analysis}</Markdown>
          </section>
        )}
      </PageBody>
    </>
  );
}
