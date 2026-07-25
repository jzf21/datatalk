"use client";

import { Info } from "lucide-react";

import type { Block, BlockDocument } from "@/lib/api/types";
import { childWidths, widthStyle } from "@/lib/doc/widths";
import { cn } from "@/lib/utils";
import { InlineMarkdown } from "@/components/markdown/inline-markdown";
import { ChartBlock } from "./chart-block";
import { DataTable } from "./data-table";
import { StatTile } from "./stat-tile";

/**
 * The single entry point for report documents, dashboard grids and Q&A
 * answers. They are the same block model, so they get the same renderer.
 *
 * `measure` constrains prose to 68ch while letting tables and charts bleed to
 * the full container -- the scientific-paper figure pattern. Dashboards pass
 * `measure={false}`: every block there is full width by design.
 */
export function DocumentView({
  document,
  measure = true,
  className,
  id = "doc",
}: {
  document: BlockDocument;
  measure?: boolean;
  className?: string;
  id?: string;
}) {
  const blocks = document?.blocks ?? [];

  if (blocks.length === 0) {
    return (
      <p className="text-[13px] text-ink-secondary">
        The generator returned no blocks.
      </p>
    );
  }

  return (
    <div id={id} className={cn("doc space-y-6", className)}>
      {blocks.map((block, i) => (
        <BlockRenderer key={i} block={block} measure={measure} />
      ))}
    </div>
  );
}

export function BlockRenderer({
  block,
  measure = true,
}: {
  block: Block;
  measure?: boolean;
}) {
  switch (block.type) {
    case "heading": {
      const level = Math.min(3, Math.max(1, block.level ?? 2));
      const Tag = (["h2", "h3", "h4"] as const)[level - 1];
      const size = [
        "text-[20px] tracking-[-0.012em]",
        "text-[17px] tracking-[-0.008em]",
        "text-[15px]",
      ][level - 1];
      return (
        <Tag
          className={cn(
            "font-semibold text-ink-primary",
            size,
            measure && "doc-measure",
          )}
        >
          <InlineMarkdown>{block.text}</InlineMarkdown>
        </Tag>
      );
    }

    case "paragraph":
      // A paragraph wrapped in underscores is a backend degradation notice --
      // a block whose dataset reference could not be resolved, degraded rather
      // than raised. It deserves to look different from prose the model wrote.
      return isDegradationNote(block.text) ? (
        <aside
          className={cn(
            "flex gap-2 border-l-2 border-border-strong py-1 pl-3 text-[13px] italic text-ink-secondary",
            measure && "doc-measure",
          )}
        >
          <Info className="mt-0.5 size-3.5 shrink-0 not-italic" aria-hidden />
          <span>
            <span className="not-italic text-ink-tertiary">
              The generator couldn&rsquo;t resolve this:{" "}
            </span>
            {block.text.replace(/^_|_$/g, "")}
          </span>
        </aside>
      ) : (
        <p
          className={cn(
            "text-[15px] leading-[1.55] text-ink-primary",
            measure && "doc-measure",
          )}
        >
          <InlineMarkdown>{block.text}</InlineMarkdown>
        </p>
      );

    case "table":
      return <DataTable block={block} />;

    case "chart":
      return <ChartBlock block={block} />;

    case "stat":
      return <StatTile block={block} />;

    case "row": {
      const children = block.children ?? [];
      const widths = childWidths(children);
      return (
        <div className="doc-row">
          {children.map((child, i) => (
            <div key={i} style={widthStyle(widths[i])}>
              {/* Inside a row the 68ch measure would fight the grid. */}
              <BlockRenderer block={child} measure={false} />
            </div>
          ))}
        </div>
      );
    }

    default: {
      // Exhaustiveness: a new block type on the backend becomes a compile
      // error here rather than a silently blank space on the page.
      const never: never = block;
      void never;
      return null;
    }
  }
}

export function isDegradationNote(text: string): boolean {
  const trimmed = text.trim();
  return trimmed.length > 2 && trimmed.startsWith("_") && trimmed.endsWith("_");
}
