import type {
  CellValue,
  ChartBlockData,
  ChartType,
  ChartUnit,
} from "@/lib/api/types";
import {
  formatCompact,
  formatExact,
  isIsoDateLike,
  parseDate,
  toNumber,
} from "@/lib/format";

export const SERIES_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
  "var(--chart-7)",
  "var(--chart-8)",
] as const;

/** The form actually rendered, which may override the backend's chart_type. */
export type ChartForm =
  | "bar"
  | "horizontal-bar"
  | "line"
  | "area"
  | "share-bar"
  | "meter"
  | "single-value"
  | "small-multiples"
  | "empty";

export type Unit = "percent" | "ratio-percent" | "currency" | "duration" | "count" | "none";

export interface SeriesMeta {
  /** `s0`, `s1`, ... -- never the raw column name. */
  key: string;
  label: string;
  color: string;
  unit: Unit;
}

export interface ChartData {
  form: ChartForm;
  /** Render the series stacked — the model asked and the data supports it. */
  stacked: boolean;
  /** One object per x value: `{ x, xLabel, s0, s1, ... }`. */
  rows: Record<string, CellValue>[];
  series: SeriesMeta[];
  xLabel: string;
  /** Synthesised: the backend sends no y-axis title. */
  yLabel: string;
  /** True when the x values parsed as dates and were sorted ascending. */
  temporal: boolean;
  /** Set when values were coerced away, so the card can say so out loud. */
  droppedValues: number;
  /** Set when categories were capped, so the card can say so out loud. */
  totalCategories: number;
}

const PERCENT_NAME = /(pct|percent|percentage|rate|ratio|share)/i;
const CURRENCY_NAME = /(revenue|amount|price|cost|spend|arr|mrr|gmv|usd|fee|charge)/i;
const DURATION_NAME = /(_ms$|millis|_s$|sec|duration|hours|hrs|_h$)/i;
const COUNT_NAME = /(count|total|^n_|num_|tickets|users|sessions|events|rows|calls)/i;

/**
 * Infer a series' unit. The backend sends none, so this is the only thing
 * standing between the reader and a "99.76" that is actually 0.9976.
 *
 * The 0..1 case only scales when the name says percent-family OR some value
 * carries >= 3 decimals -- the signature of `avg(bool)` / `countIf/count`.
 * Even then the raw value travels with it into every tooltip and table, so the
 * inference is never lossy and never unverifiable.
 */
export function inferUnit(name: string, values: number[]): Unit {
  const named = PERCENT_NAME.test(name);
  if (values.length > 0 && values.every((v) => v >= 0 && v <= 1)) {
    const precise = values.some((v) => String(v).split(".")[1]?.length >= 3);
    if (named || precise) return "ratio-percent";
  }
  if (named) return "percent";
  if (CURRENCY_NAME.test(name)) return "currency";
  if (DURATION_NAME.test(name)) return "duration";
  if (COUNT_NAME.test(name)) return "count";
  return "none";
}

/**
 * An explicit model-declared unit beats name inference. `"ratio"` asserts the
 * values are 0..1 fractions; when a value disproves that, fall back to plain
 * `percent` (suffix without scaling) — an asserted ratio that is not one must
 * not display 45 as 4500%.
 */
export function resolveUnit(
  declared: ChartUnit | undefined,
  inferred: Unit,
  values: number[],
): Unit {
  if (declared === "ratio") {
    return values.every((v) => v >= 0 && v <= 1) ? "ratio-percent" : "percent";
  }
  if (
    declared === "percent" ||
    declared === "currency" ||
    declared === "duration" ||
    declared === "count"
  ) {
    return declared;
  }
  return inferred;
}

export function scaleForUnit(value: number, unit: Unit): number {
  return unit === "ratio-percent" ? value * 100 : value;
}

/** Axes and stat tiles are compact; tooltips and tables are exact. */
export function formatAxisValue(value: number, unit: Unit): string {
  const scaled = scaleForUnit(value, unit);
  if (unit === "percent" || unit === "ratio-percent") return `${formatCompact(scaled)}%`;
  if (unit === "currency") return `$${formatCompact(scaled)}`;
  return formatCompact(scaled);
}

export function formatTooltipValue(value: number, unit: Unit): string {
  const scaled = scaleForUnit(value, unit);
  if (unit === "ratio-percent") {
    // Belt and braces: the inference is always shown next to its input.
    return `${formatPercent(scaled)}% (${value})`;
  }
  if (unit === "percent") return `${formatPercent(scaled)}%`;
  if (unit === "currency") return `$${formatExact(scaled)}`;
  return formatExact(scaled);
}

/**
 * Percentages keep two decimals regardless of magnitude. The general
 * significant-digit rule drops to one decimal above 10, which would round a
 * 99.76% success rate to 99.8% -- erasing precisely the digits an SLA figure
 * is read for.
 */
function formatPercent(scaled: number): string {
  return scaled.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

function unitSuffix(unit: Unit): string {
  if (unit === "percent" || unit === "ratio-percent") return " (%)";
  if (unit === "currency") return " ($)";
  return "";
}

function titleCase(name: string): string {
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

const MAX_CATEGORIES = 25;
const CATEGORY_CAP_THRESHOLD = 30;
const LONG_LABEL = 14;

/**
 * Turn a materialized chart block into everything the renderer needs.
 *
 * Series are keyed `s0..sN` rather than by column name: shadcn's ChartStyle
 * interpolates config keys into CSS custom-property names, and LLM-chosen
 * column names contain spaces, dots and quotes that would emit invalid CSS.
 */
export function toChartData(block: ChartBlockData): ChartData {
  const xValues = block.x?.values ?? [];
  const rawSeries = block.series ?? [];

  if (xValues.length === 0 || rawSeries.length === 0) {
    return {
      form: "empty",
      stacked: false,
      rows: [],
      series: [],
      xLabel: block.x?.label ?? "",
      yLabel: "",
      temporal: false,
      droppedValues: 0,
      totalCategories: 0,
    };
  }

  // Numeric coercion at the boundary. Recharts renders a silent flat line for
  // stringified Decimals, so a bad value must be dropped visibly, not drawn.
  let dropped = 0;
  const numeric = rawSeries.map((s) =>
    (s.values ?? []).map((v) => {
      const n = toNumber(v);
      if (n === null && v !== null && v !== "") dropped += 1;
      return n;
    }),
  );

  const series: SeriesMeta[] = rawSeries.map((s, i) => {
    const values = numeric[i].filter((n): n is number => n !== null);
    return {
      key: `s${i}`,
      label: s.name,
      // Colour follows the entity in delivered order, never its rank -- so
      // isolating or re-sorting never repaints the survivors.
      color: SERIES_COLORS[i % SERIES_COLORS.length],
      unit: resolveUnit(block.unit, inferUnit(s.name, values), values),
    };
  });

  const temporal = xValues.every(isIsoDateLike);

  let indices = xValues.map((_, i) => i);
  if (temporal) {
    // ClickHouse row order is not guaranteed to be reading order.
    indices = indices.sort(
      (a, b) =>
        (parseDate(xValues[a])?.getTime() ?? 0) -
        (parseDate(xValues[b])?.getTime() ?? 0),
    );
  }

  const totalCategories = indices.length;
  let capped = indices;
  if (!temporal && totalCategories > CATEGORY_CAP_THRESHOLD) {
    // Keep the biggest stories; the Table twin always holds everything.
    capped = [...indices]
      .sort((a, b) => maxAt(numeric, b) - maxAt(numeric, a))
      .slice(0, MAX_CATEGORIES);
  }

  const rows = capped.map((i) => {
    const row: Record<string, CellValue> = {
      x: temporal ? formatTemporalTick(xValues, i) : String(xValues[i] ?? ""),
      xRaw: xValues[i],
    };
    series.forEach((s, si) => {
      row[s.key] = numeric[si][i];
    });
    return row;
  });

  const units = new Set(series.map((s) => s.unit));
  const yLabel =
    series.length === 1
      ? titleCase(series[0].label) + unitSuffix(series[0].unit)
      : units.size === 1
        ? unitSuffix([...units][0]).replace(/[() ]/g, "")
        : "";

  const form = inferForm(block, rows.length, series, units, temporal);
  const stacked =
    block.stacked === true &&
    series.length >= 2 &&
    units.size === 1 &&
    (form === "bar" || form === "horizontal-bar" || form === "area");

  return {
    form,
    stacked,
    rows,
    series,
    xLabel: block.x?.label ?? "",
    yLabel,
    temporal,
    droppedValues: dropped,
    totalCategories,
  };
}

function maxAt(numeric: (number | null)[][], i: number): number {
  return Math.max(...numeric.map((vals) => vals[i] ?? Number.NEGATIVE_INFINITY));
}

/**
 * Pick the form the data actually supports, which is not always the one the
 * model asked for. Precedence: data impossibilities, then honesty guardrails
 * (regardless of what was requested), then the model's hint when the data
 * supports it, then heuristics. An unknown chart_type falls through to "bar",
 * which is what keeps new documents safe on an old cached bundle.
 */
export function inferForm(
  block: ChartBlockData,
  categories: number,
  series: SeriesMeta[],
  units: Set<Unit>,
  temporal: boolean,
): ChartForm {
  const requested: ChartType = block.chart_type ?? "bar";
  const stackable =
    block.stacked === true && series.length >= 2 && units.size === 1;

  if (categories === 0 || series.length === 0) return "empty";
  // A one-bar bar chart is never the right answer.
  if (categories === 1 && series.length === 1) return "single-value";

  if (requested === "pie") {
    if (categories <= 2) return "meter";
    return "share-bar";
  }

  // Never a dual axis -- the most common charting lie.
  if (series.length > 6 || units.size > 1) return "small-multiples";

  // The model's hint, honored when the data supports it: a horizontal time
  // axis reads wrong, so a temporal x falls back to vertical bars.
  if (requested === "horizontal_bar") {
    return temporal ? "bar" : "horizontal-bar";
  }

  // Three overlapping washes are unreadable -- but stacked areas tile, so a
  // stacking request lifts the demotion.
  if (requested === "area" && series.length >= 3 && !stackable) return "line";

  const labelsAreLong = block.x.values.some(
    (v) => String(v ?? "").length > LONG_LABEL,
  );
  if (requested === "bar" && (labelsAreLong || categories > 12)) {
    // Rotating a tick is the smell that means "should have been horizontal".
    return "horizontal-bar";
  }

  if (requested === "area") return "area";
  if (requested === "line") return "line";
  return "bar";
}

/** Granularity inferred from the modal gap between consecutive points. */
function formatTemporalTick(values: CellValue[], index: number): string {
  const date = parseDate(values[index]);
  if (!date) return String(values[index] ?? "");

  const parsed = values
    .map(parseDate)
    .filter((d): d is Date => d !== null)
    .map((d) => d.getTime())
    .sort((a, b) => a - b);

  const gaps = parsed.slice(1).map((t, i) => t - parsed[i]);
  const median = gaps.length > 0 ? gaps.sort((a, b) => a - b)[gaps.length >> 1] : 0;

  const DAY = 86_400_000;
  if (median >= 28 * DAY) {
    return date.toLocaleDateString(undefined, { month: "short", year: "numeric" });
  }
  if (median >= 20 * 3_600_000) {
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  if (median >= 50 * 60_000) {
    return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** The full timestamp, always shown in the tooltip. */
export function formatTemporalFull(value: CellValue): string {
  const date = parseDate(value);
  if (!date) return String(value ?? "");
  return date.toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
