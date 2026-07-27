"use client";

import type { Insight, InsightSet } from "@/lib/api/types";
import { cn } from "@/lib/utils";

/**
 * The insight pass's findings, readable on their own. During generation they
 * are the only real content available while the author call runs — the longest
 * silent stretch of the pipeline — and on a saved dashboard they are the
 * record of why the grid leads with what it leads with.
 */
export function InsightPanel({
  insights,
  className,
}: {
  insights: InsightSet;
  className?: string;
}) {
  const findings = (insights.insights ?? []).filter(
    (i): i is Insight => typeof i === "object" && i !== null && !!i.finding,
  );
  const gaps = (insights.gaps ?? []).filter(Boolean);
  if (findings.length === 0 && gaps.length === 0) return null;

  const lead = new Set(insights.lead ?? []);
  const ordered = [...findings].sort(
    (a, b) => (b.importance ?? 0) - (a.importance ?? 0),
  );

  return (
    <section className={cn("space-y-3", className)}>
      <h2 className="label-caps text-ink-tertiary">What the data shows</h2>
      <ul className="space-y-2">
        {ordered.map((f, i) => (
          <li key={i} className="flex items-baseline gap-2 text-[13px]">
            {f.dataset_id && (
              <span
                className={cn(
                  "cite shrink-0 rounded-[2px] px-1 leading-none",
                  lead.has(f.dataset_id)
                    ? "bg-ink-primary text-background"
                    : "bg-accent text-ink-secondary",
                )}
              >
                {f.dataset_id}
              </span>
            )}
            <span className="text-ink-primary">
              {f.finding}
              {f.kind && (
                <span className="ml-1.5 text-[12px] text-ink-tertiary">
                  {f.kind}
                </span>
              )}
            </span>
          </li>
        ))}
      </ul>
      {gaps.length > 0 && (
        <p className="text-[12px] text-ink-tertiary">
          Not answerable from this data: {gaps.join(" · ")}
        </p>
      )}
    </section>
  );
}
