"use client";

import { cn } from "@/lib/utils";
import { useSources } from "./sources-context";

/**
 * The tie-out mark.
 *
 * DataTalk's core promise is that the LLM never types numbers -- every figure
 * is materialized from a captured dataset. This chip is that promise made
 * visible: it names the query behind the block it sits on, highlights that
 * query in the Sources rail on hover, and pins it on click.
 *
 * Absent on documents generated before `dataset_id` survived materialization;
 * provenance then degrades to the rail's flat query list, with no branching
 * anywhere else in the renderer.
 */
export function TickChip({
  datasetId,
  variant = "paper",
  className,
}: {
  datasetId?: string;
  /** "terminal" wears the pulse-teal badge on the dark data plane. */
  variant?: "paper" | "terminal";
  className?: string;
}) {
  const sources = useSources();
  if (!datasetId) return null;

  const active = sources?.active === datasetId;

  return (
    <button
      type="button"
      onMouseEnter={() => sources?.hover(datasetId)}
      onMouseLeave={() => sources?.hover(null)}
      onFocus={() => sources?.hover(datasetId)}
      onBlur={() => sources?.hover(null)}
      onClick={() => sources?.pin(datasetId)}
      aria-label={`Show the query behind this: ${datasetId}`}
      className={cn(
        "cite rounded-[2px] px-1 py-px leading-none transition-colors duration-[120ms]",
        variant === "terminal"
          ? active
            ? "bg-pulse/25 text-pulse"
            : "bg-pulse/12 text-pulse hover:bg-pulse/20"
          : active
            ? "bg-ring/12 text-link"
            : "text-ink-tertiary hover:bg-accent hover:text-ink-primary",
        className,
      )}
    >
      {datasetId}
    </button>
  );
}
