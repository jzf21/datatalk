import type { CellValue } from "./api/types";

/**
 * Coerce a wire value to a finite number, or null.
 *
 * Not optional at the chart boundary: ClickHouse Decimals arrive as strings
 * (`json.dumps(default=str)`), and Recharts renders a silent flat line for
 * those rather than failing loudly the way Chart.js's coercion did.
 */
export function toNumber(value: CellValue): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?/;

export function isIsoDateLike(value: CellValue): boolean {
  return typeof value === "string" && ISO_DATE.test(value);
}

export function parseDate(value: CellValue): Date | null {
  if (!isIsoDateLike(value)) return null;
  const d = new Date(value as string);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * The system rule, applied everywhere: axes and stat tiles are COMPACT,
 * tooltips and tables are EXACT. `12.4K` on the tick, `12,438` in the tooltip.
 */
export function formatCompact(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000_000) return `${trim(n / 1_000_000_000)}B`;
  if (abs >= 1_000_000) return `${trim(n / 1_000_000)}M`;
  if (abs >= 10_000) return `${trim(n / 1_000)}K`;
  return formatExact(n);
}

function trim(n: number): string {
  return n.toFixed(Math.abs(n) >= 100 ? 0 : 1).replace(/\.0$/, "");
}

export function formatExact(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  // Very small non-zero magnitudes would otherwise round away to "0".
  if (abs > 0 && abs < 0.01) return n.toPrecision(3);
  const decimals = abs >= 100 ? 0 : abs >= 10 ? 1 : 2;
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals,
  });
}

/** Grouped thousands with a fixed decimal count -- for table columns. */
export function formatFixed(n: number, decimals: number): string {
  return n.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/** How many decimal places a value is written with, capped. */
export function decimalPlaces(value: CellValue, cap = 4): number {
  const s = typeof value === "number" ? String(value) : String(value ?? "");
  const dot = s.indexOf(".");
  if (dot === -1) return 0;
  return Math.min(cap, s.length - dot - 1);
}

export function formatDateTime(value: CellValue): string {
  const d = parseDate(value);
  if (!d) return String(value ?? "");
  const hasTime = /[T ]\d{2}:\d{2}/.test(String(value));
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(hasTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  });
}

export function formatRelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} d ago`;
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

/** "2,104 rows · 6 columns" -- the mono provenance line above a table. */
export function formatCount(n: number, singular: string, plural = `${singular}s`) {
  return `${n.toLocaleString()} ${n === 1 ? singular : plural}`;
}
