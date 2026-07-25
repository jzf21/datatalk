import type { Block } from "@/lib/api/types";

const COLUMNS = 12;

/**
 * Resolve the column span of every child in a row.
 *
 * Fixes two bugs in the legacy renderer:
 *
 * 1. Even split used `round(12 / n)`. Five children each got 2, leaving a
 *    2-column hole on the right. We distribute the remainder instead, so the
 *    widths always sum to exactly 12.
 * 2. Author-supplied widths summing over 12 made the CSS grid wrap mid-row,
 *    silently breaking the layout. We scale them back proportionally.
 */
export function childWidths(children: Block[]): number[] {
  const n = children.length;
  if (n === 0) return [];

  const declared = children.map((c) =>
    "width" in c && typeof c.width === "number" ? clamp(c.width) : null,
  );

  // Nothing declared: an even split, remainder handed to the leftmost cells.
  if (declared.every((w) => w === null)) {
    const base = Math.floor(COLUMNS / n);
    const remainder = COLUMNS % n;
    return Array.from({ length: n }, (_, i) =>
      Math.max(1, base + (i < remainder ? 1 : 0)),
    );
  }

  // Mixed: undeclared children share whatever the declared ones left over.
  const declaredTotal = declared.reduce<number>((sum, w) => sum + (w ?? 0), 0);
  const undeclared = declared.filter((w) => w === null).length;
  const leftover = Math.max(0, COLUMNS - declaredTotal);
  const fallback = undeclared > 0 ? Math.max(1, Math.floor(leftover / undeclared)) : 0;

  const widths = declared.map((w) => w ?? fallback);
  const total = widths.reduce((a, b) => a + b, 0);
  if (total <= COLUMNS) return widths;

  // Over-wide: scale proportionally rather than let the grid wrap.
  return widths.map((w) => clamp(Math.round((w * COLUMNS) / total)));
}

/**
 * The tablet step. A 4-up KPI row becomes 2-up and a 6-wide chart goes full
 * width, rather than every cell halving into something unreadable.
 */
export function mediumWidth(width: number): number {
  return width <= 4 ? 6 : COLUMNS;
}

function clamp(width: number): number {
  return Math.max(1, Math.min(COLUMNS, Math.round(width)));
}

/** Inline custom properties consumed by the `.doc-row` container queries. */
export function widthStyle(width: number): React.CSSProperties {
  return {
    "--w": width,
    "--w-md": mediumWidth(width),
  } as React.CSSProperties;
}
