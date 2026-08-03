import type { CellValue } from "@/lib/api/types";
import {
  decimalPlaces,
  formatDateTime,
  formatFixed,
  isIsoDateLike,
  toNumber,
} from "@/lib/format";

export type ColumnKind = "number" | "datetime" | "boolean" | "text";

export interface ColumnMeta {
  name: string;
  kind: ColumnKind;
  /** Decimal places to render, taken from the most precise value in the column. */
  decimals: number;
  /** Name-inferred ratio column (0..1) that should read as a percentage. */
  percent: boolean;
  /**
   * Numeric column containing at least one negative value. Mixed signs mean
   * the column is a delta/amount, so its values render as credit/debit pills;
   * unsigned measures stay plain -- colouring every number would be noise.
   */
  signed: boolean;
}

const PERCENT_NAME = /(pct|percent|percentage|rate|ratio|share)/i;

/**
 * Infer a column's type from ALL its values, not the first one -- a column
 * whose first row happens to be null would otherwise be typed as text and lose
 * its right alignment.
 */
export function inferColumns(
  columns: string[],
  rows: CellValue[][],
): ColumnMeta[] {
  return columns.map((name, i) => {
    const values = rows.map((row) => row[i]).filter((v) => v !== null && v !== "");

    const kind: ColumnKind =
      values.length === 0
        ? "text"
        : values.every((v) => typeof v === "boolean")
          ? "boolean"
          : values.every((v) => toNumber(v) !== null)
            ? "number"
            : values.every(isIsoDateLike)
              ? "datetime"
              : "text";

    const decimals =
      kind === "number"
        ? values.reduce<number>((max, v) => Math.max(max, decimalPlaces(v)), 0)
        : 0;

    // A 0..1 column named like a rate is a proportion the user reads as a
    // percentage. Requiring the name match keeps genuine 0..1 measurements
    // (a score, a coefficient) from being silently multiplied by 100.
    const percent =
      kind === "number" &&
      PERCENT_NAME.test(name) &&
      values.length > 0 &&
      values.every((v) => {
        const n = toNumber(v);
        return n !== null && n >= 0 && n <= 1;
      });

    const signed =
      kind === "number" && values.some((v) => (toNumber(v) ?? 0) < 0);

    return { name, kind, decimals, percent, signed };
  });
}

export function formatCell(value: CellValue, meta: ColumnMeta): string {
  // An empty cell reads as a rendering bug; an em dash reads as "no value".
  if (value === null || value === "") return "—";
  if (meta.kind === "boolean") return value ? "true" : "false";
  if (meta.kind === "datetime") return formatDateTime(value);
  if (meta.kind === "number") {
    const n = toNumber(value);
    if (n === null) return String(value);
    if (meta.percent) return `${formatFixed(n * 100, 2)}%`;
    return formatFixed(n, meta.decimals);
  }
  return String(value);
}

/**
 * The raw value, shown alongside any inferred formatting. The percent
 * inference must never be lossy or unverifiable -- that is the whole product
 * thesis -- so the untouched number is always one hover away.
 */
export function rawCellTitle(value: CellValue, meta: ColumnMeta): string | undefined {
  return meta.percent ? String(value) : undefined;
}

export function isNumericColumn(meta: ColumnMeta): boolean {
  return meta.kind === "number";
}
