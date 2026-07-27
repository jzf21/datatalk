import { ArrowDown, ArrowUp } from "lucide-react";

import type { StatBlock } from "@/lib/api/types";
import { deltaTone } from "@/lib/doc/stat";
import { formatCompact, toNumber } from "@/lib/format";
import { cn } from "@/lib/utils";
import { TickChip } from "./tick-chip";

/**
 * A KPI as a ledger entry, not a gradient card: hairline rule, small-caps
 * label, proportional figures, delta with a glyph *and* a word. No icon
 * container, no gradient, no colour on the surface -- colour is for data.
 */
export function StatTile({ block }: { block: StatBlock }) {
  const value = toNumber(block.value ?? null);
  const display =
    value === null ? String(block.value ?? "—") : formatCompact(value);

  const delta = block.delta ?? null;
  const deltaPct = block.delta_pct ?? null;
  const rising = delta !== null && delta > 0;
  const falling = delta !== null && delta < 0;
  // The arrow and word follow the sign; the colour follows goodness, because
  // a falling churn rate is the good news.
  const tone = deltaTone(delta, block.direction);

  return (
    <div className="flex h-full flex-col border-t border-border-strong pt-3">
      <div className="flex items-start justify-between gap-2">
        <h3 className="label-caps text-ink-secondary">{block.label}</h3>
        <TickChip datasetId={block.dataset_id} className="-mt-px" />
      </div>

      {/* Proportional figures, never tabular: tabular-nums is for values
          stacked in a column, and a hero figure stands alone. */}
      <p className="mt-2 text-[30px] font-semibold leading-[1.05] tracking-[-0.02em] text-ink-primary">
        {display}
        {block.unit && (
          <span className="ml-1 text-[15px] font-normal text-ink-secondary">
            {block.unit}
          </span>
        )}
      </p>

      {delta !== null && (
        <p
          className={cn(
            "mt-1.5 flex items-center gap-1 text-[12px]",
            tone === "good" && "text-status-good-text",
            tone === "bad" && "text-destructive",
            tone === "neutral" && "text-ink-secondary",
          )}
        >
          {rising && <ArrowUp className="size-3" aria-hidden />}
          {falling && <ArrowDown className="size-3" aria-hidden />}
          {/* Never colour alone -- the direction is also a word. */}
          <span className="sr-only">{rising ? "up" : falling ? "down" : "flat"} </span>
          <span className="tabular-nums">
            {formatCompact(Math.abs(delta))}
            {deltaPct !== null && ` (${Math.abs(deltaPct).toFixed(1)}%)`}
          </span>
          <span className="text-ink-tertiary">vs prior</span>
        </p>
      )}
    </div>
  );
}
