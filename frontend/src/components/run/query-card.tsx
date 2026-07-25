"use client";

import { useState } from "react";
import { ChevronRight, Copy, TriangleAlert } from "lucide-react";
import { toast } from "sonner";

import type { QueryStep } from "@/hooks/use-ndjson-run";
import { cn } from "@/lib/utils";

/**
 * One SQL step. Collapsed to a single line while running, resolving in place
 * to its row count -- an indeterminate bar rather than a spinner, because the
 * bar says "work is arriving" where a spinner only says "wait".
 */
export function QueryCard({
  step,
  highlighted,
  onHover,
}: {
  step: QueryStep;
  highlighted?: boolean;
  onHover?: (datasetId: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const running = step.state === "running";

  return (
    <div
      onMouseEnter={() => step.datasetId && onHover?.(step.datasetId)}
      onMouseLeave={() => onHover?.(null)}
      className={cn(
        "border-b border-border last:border-b-0",
        highlighted && "bg-ring/8",
      )}
    >
      <div className="flex items-center gap-2 px-3 py-2">
        <span
          className={cn(
            "cite shrink-0 rounded-[2px] px-1 leading-none",
            running
              ? "border border-dashed border-border-strong text-ink-disabled"
              : "bg-accent text-ink-secondary",
          )}
        >
          {step.datasetId ?? "··"}
        </span>

        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          aria-expanded={open}
        >
          <ChevronRight
            aria-hidden
            className={cn(
              "size-3 shrink-0 text-ink-tertiary transition-transform duration-[120ms]",
              open && "rotate-90",
            )}
          />
          <code className="cite truncate text-ink-secondary">
            {step.sql.replace(/\s+/g, " ").slice(0, 72)}
            {step.sql.length > 72 && "…"}
          </code>
        </button>

        {running ? (
          <span
            className="h-[3px] w-12 shrink-0 overflow-hidden rounded-full bg-muted"
            role="progressbar"
            aria-label="Query running"
          >
            <span className="shimmer block h-full w-full" />
          </span>
        ) : (
          <span className="cite shrink-0 text-ink-tertiary">
            {step.rowCount?.toLocaleString()} rows · {step.columns?.length} cols
          </span>
        )}
      </div>

      {/* A retry is the analyst correcting course, not a break. Amber rule,
          inline, glyph AND word -- status is never colour alone. */}
      {step.retries.map((message, i) => (
        <p
          key={i}
          className="mx-3 mb-2 flex gap-1.5 border-l-2 border-[var(--status-warning)] py-0.5 pl-2 text-[12px] text-ink-secondary"
        >
          <TriangleAlert
            className="mt-px size-3 shrink-0 text-[var(--status-warning)]"
            aria-hidden
          />
          <span>
            <span className="font-medium text-ink-primary">retried</span> —{" "}
            <span className="cite line-clamp-2">{message}</span>
          </span>
        </p>
      ))}

      {open && (
        <div className="px-3 pb-3">
          <pre className="cite max-h-64 overflow-auto rounded-[4px] border border-border bg-muted p-2 text-ink-primary">
            {step.sql}
          </pre>
          <div className="mt-2 flex flex-wrap items-center gap-1">
            <button
              type="button"
              onClick={() => {
                navigator.clipboard.writeText(step.sql);
                toast.success("SQL copied");
              }}
              className="flex items-center gap-1 rounded-[4px] px-1.5 py-0.5 text-[12px] text-ink-secondary hover:bg-accent hover:text-ink-primary"
            >
              <Copy className="size-3" aria-hidden /> Copy
            </button>
            {step.columns?.map((c) => (
              <span
                key={c}
                className="cite rounded-[2px] bg-muted px-1 text-ink-tertiary"
              >
                {c}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
