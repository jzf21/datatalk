"use client";

import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  XAxis,
  YAxis,
} from "recharts";

import type { ChartBlockData } from "@/lib/api/types";
import {
  formatAxisValue,
  formatTemporalFull,
  formatTooltipValue,
  toChartData,
  type ChartData,
  type SeriesMeta,
} from "@/lib/doc/chart-data";
import { formatCompact } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { DataTable } from "./data-table";
import { TickChip } from "./tick-chip";

export function ChartBlock({ block }: { block: ChartBlockData }) {
  const data = useMemo(() => toChartData(block), [block]);
  const [view, setView] = useState<"chart" | "table">("chart");

  const config = useMemo<ChartConfig>(
    () =>
      Object.fromEntries(
        data.series.map((s) => [s.key, { label: s.label, color: s.color }]),
      ),
    [data.series],
  );

  const title = block.title || data.series.map((s) => s.label).join(", ");

  return (
    <figure className="rounded-[6px] border border-border bg-card">
      <figcaption className="flex items-start justify-between gap-3 px-4 pt-3">
        <div className="min-w-0">
          <h3 className="truncate text-[15px] font-semibold text-ink-primary">
            {title}
          </h3>
          {/* Legend sits under the title, next to what it names -- a bottom
              legend squeezes the plot and drifts away from its subject. */}
          {data.series.length > 1 && (
            <ul className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
              {data.series.map((s) => (
                <li
                  key={s.key}
                  className="flex items-center gap-1.5 text-[12px] text-ink-secondary"
                >
                  <span
                    aria-hidden
                    className="inline-block size-2.5 rounded-[2px]"
                    style={{ background: s.color }}
                  />
                  {s.label}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <TickChip datasetId={block.dataset_id} />
          {/* The relief channel. Three palette hues sit under 3:1 against the
              card, so every chart ships a WCAG-clean twin -- visible, keyboard
              reachable, never hidden behind hover. */}
          <ToggleGroup
            type="single"
            size="sm"
            value={view}
            onValueChange={(v) => v && setView(v as "chart" | "table")}
            aria-label="Chart or table view"
          >
            <ToggleGroupItem value="chart" className="h-6 px-2 text-[12px]">
              Chart
            </ToggleGroupItem>
            <ToggleGroupItem value="table" className="h-6 px-2 text-[12px]">
              Table
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
      </figcaption>

      <div className="px-4 pb-3 pt-3">
        {view === "table" ? (
          <TableTwin block={block} data={data} />
        ) : (
          <ChartFigure data={data} config={config} />
        )}
      </div>

      <Caveats data={data} />
    </figure>
  );
}

function ChartFigure({
  data,
  config,
}: {
  data: ChartData;
  config: ChartConfig;
}) {
  if (data.form === "empty") {
    return (
      <p className="py-8 text-center text-[13px] text-ink-secondary">
        No rows returned.
      </p>
    );
  }

  if (data.form === "single-value") {
    const value = data.rows[0]?.[data.series[0].key];
    return (
      <div className="py-4">
        <p className="text-[30px] font-semibold leading-[1.05] tracking-[-0.02em] text-ink-primary">
          {typeof value === "number"
            ? formatAxisValue(value, data.series[0].unit)
            : "—"}
        </p>
        <p className="mt-1 text-[12px] text-ink-secondary">{data.rows[0]?.x}</p>
      </div>
    );
  }

  if (data.form === "share-bar" || data.form === "meter") {
    return <ShareBar data={data} />;
  }

  if (data.form === "small-multiples") {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {data.series.map((s) => (
          <div key={s.key}>
            <p className="mb-1 truncate text-[12px] text-ink-secondary">
              {s.label}
            </p>
            <Facet data={data} series={s} config={config} />
          </div>
        ))}
      </div>
    );
  }

  return <MainChart data={data} config={config} />;
}

const AXIS_TICK = { fill: "var(--ink-secondary)", fontSize: 11 };

function MainChart({ data, config }: { data: ChartData; config: ChartConfig }) {
  const horizontal = data.form === "horizontal-bar";
  const unit = data.series[0]?.unit ?? "none";

  const common = (
    <>
      {/* Solid, never dashed: shadcn's default 3-3 dash reads as a threshold. */}
      <CartesianGrid
        vertical={horizontal}
        horizontal={!horizontal}
        stroke="var(--chart-grid)"
        strokeDasharray="0"
      />
      <ChartTooltip
        cursor={{ fill: "var(--muted)" }}
        content={<Tooltip data={data} />}
      />
    </>
  );

  const axes = horizontal ? (
    <>
      <XAxis
        type="number"
        tick={AXIS_TICK}
        tickLine={false}
        axisLine={false}
        tickFormatter={(v: number) => formatAxisValue(v, unit)}
      />
      <YAxis
        type="category"
        dataKey="x"
        width={140}
        tick={AXIS_TICK}
        tickLine={false}
        axisLine={{ stroke: "var(--chart-axis)" }}
      />
    </>
  ) : (
    <>
      <XAxis
        dataKey="x"
        tick={AXIS_TICK}
        tickLine={false}
        axisLine={{ stroke: "var(--chart-axis)" }}
      />
      <YAxis
        tick={AXIS_TICK}
        tickLine={false}
        axisLine={false}
        width={56}
        tickFormatter={(v: number) => formatAxisValue(v, unit)}
      />
    </>
  );

  const height = horizontal
    ? Math.max(200, data.rows.length * 26 + 40)
    : undefined;

  if (data.form === "line") {
    return (
      <ChartContainer config={config} className="min-h-[240px] w-full">
        <LineChart data={data.rows} margin={{ left: 4, right: 12, top: 8 }}>
          {common}
          {axes}
          {data.series.map((s) => (
            <Line
              key={s.key}
              dataKey={s.key}
              stroke={s.color}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              // Linear, not monotone: a spline invents values between monthly
              // aggregates that were never measured.
              type="linear"
              // A gap in the data is a gap on screen.
              connectNulls={false}
              dot={data.rows.length <= 12 ? { r: 3, strokeWidth: 2, stroke: "var(--card)" } : false}
              activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ChartContainer>
    );
  }

  if (data.form === "area") {
    return (
      <ChartContainer config={config} className="min-h-[240px] w-full">
        <AreaChart data={data.rows} margin={{ left: 4, right: 12, top: 8 }}>
          {common}
          {axes}
          {data.series.map((s) => (
            <Area
              key={s.key}
              dataKey={s.key}
              stroke={s.color}
              strokeWidth={2}
              fill={s.color}
              // Flat opacity, never a gradient: gradients are decoration.
              fillOpacity={data.series.length === 1 ? 0.1 : 0.08}
              type="linear"
              connectNulls={false}
              isAnimationActive={false}
            />
          ))}
        </AreaChart>
      </ChartContainer>
    );
  }

  return (
    <ChartContainer
      config={config}
      className="w-full"
      style={height ? { height } : undefined}
    >
      <BarChart
        data={data.rows}
        layout={horizontal ? "vertical" : "horizontal"}
        margin={{ left: 4, right: 12, top: 8 }}
        barCategoryGap="28%"
        barGap={2}
      >
        {common}
        {axes}
        {data.series.map((s) => (
          <Bar
            key={s.key}
            dataKey={s.key}
            fill={s.color}
            // Rounded data-end, square at the baseline.
            radius={horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]}
            maxBarSize={24}
            isAnimationActive={false}
          />
        ))}
      </BarChart>
    </ChartContainer>
  );
}

/** A one-series mini with its own axis, used for small multiples. */
function Facet({
  data,
  series,
  config,
}: {
  data: ChartData;
  series: SeriesMeta;
  config: ChartConfig;
}) {
  return (
    <ChartContainer config={config} className="min-h-[140px] w-full">
      <BarChart data={data.rows} margin={{ left: 0, right: 4, top: 4 }}>
        <CartesianGrid vertical={false} stroke="var(--chart-grid)" strokeDasharray="0" />
        <XAxis dataKey="x" tick={{ ...AXIS_TICK, fontSize: 10 }} tickLine={false} axisLine={false} />
        <YAxis
          tick={{ ...AXIS_TICK, fontSize: 10 }}
          tickLine={false}
          axisLine={false}
          width={44}
          tickFormatter={(v: number) => formatAxisValue(v, series.unit)}
        />
        <ChartTooltip content={<Tooltip data={data} only={series.key} />} />
        <Bar dataKey={series.key} fill={series.color} radius={[4, 4, 0, 0]} maxBarSize={20} isAnimationActive={false} />
      </BarChart>
    </ChartContainer>
  );
}

/** A pie, rendered honestly: one 100% bar, segments gapped by the surface. */
function ShareBar({ data }: { data: ChartData }) {
  const key = data.series[0].key;
  const unit = data.series[0].unit;
  const slices = data.rows
    .map((row) => ({ label: String(row.x), value: Number(row[key]) || 0 }))
    .filter((s) => s.value > 0);
  const total = slices.reduce((sum, s) => sum + s.value, 0);

  if (total <= 0) {
    return (
      <p className="py-6 text-center text-[13px] text-ink-secondary">
        All values are zero.
      </p>
    );
  }

  const shown = slices.slice(0, 6);
  const rest = slices.slice(6);
  const segments = [
    ...shown.map((s, i) => ({ ...s, color: `var(--chart-${(i % 8) + 1})` })),
    ...(rest.length > 0
      ? [
          {
            label: `Other (${rest.length})`,
            value: rest.reduce((sum, s) => sum + s.value, 0),
            color: "var(--chart-deemph)",
          },
        ]
      : []),
  ];

  return (
    <div>
      <div className="flex h-7 w-full gap-[2px] overflow-hidden">
        {segments.map((seg) => (
          <div
            key={seg.label}
            title={`${seg.label}: ${formatTooltipValue(seg.value, unit)}`}
            style={{
              width: `${(seg.value / total) * 100}%`,
              background: seg.color,
            }}
            className="first:rounded-l-[4px] last:rounded-r-[4px]"
          />
        ))}
      </div>
      <ul className="mt-3 flex flex-wrap gap-x-4 gap-y-1">
        {segments.map((seg) => (
          <li key={seg.label} className="flex items-center gap-1.5 text-[12px]">
            <span
              aria-hidden
              className="inline-block size-2.5 rounded-[2px]"
              style={{ background: seg.color }}
            />
            <span className="text-ink-secondary">{seg.label}</span>
            <span className="tabular-nums text-ink-primary">
              {((seg.value / total) * 100).toFixed(1)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

interface TooltipPayload {
  dataKey?: string | number;
  value?: number;
  payload?: Record<string, unknown>;
}

/** Values lead, labels follow. Exact numbers, never the compact axis form. */
function Tooltip({
  data,
  only,
  active,
  payload,
}: {
  data: ChartData;
  only?: string;
  active?: boolean;
  payload?: TooltipPayload[];
}) {
  if (!active || !payload?.length) return null;

  const raw = payload[0]?.payload?.xRaw;
  const header = data.temporal
    ? formatTemporalFull(raw as string)
    : String(payload[0]?.payload?.x ?? "");

  const rows = payload.filter((p) => !only || p.dataKey === only);

  return (
    <div className="rounded-[6px] border border-border bg-popover px-2.5 py-2 shadow-[var(--shadow-overlay)]">
      <p className="mb-1 text-[12px] text-ink-secondary">{header}</p>
      <ul className="space-y-0.5">
        {rows.map((row) => {
          const series = data.series.find((s) => s.key === row.dataKey);
          if (!series) return null;
          return (
            <li
              key={series.key}
              className="flex items-baseline justify-between gap-4 text-[13px]"
            >
              <span className="flex items-center gap-1.5 text-ink-secondary">
                <span
                  aria-hidden
                  className="inline-block h-0.5 w-3 rounded-full"
                  style={{ background: series.color }}
                />
                {series.label}
              </span>
              <span className="font-semibold tabular-nums text-ink-primary">
                {typeof row.value === "number"
                  ? formatTooltipValue(row.value, series.unit)
                  : "—"}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** The table twin: the same numbers, in the accessible form. */
function TableTwin({ block, data }: { block: ChartBlockData; data: ChartData }) {
  const columns = [block.x?.label || "x", ...data.series.map((s) => s.label)];
  const rows = data.rows.map((row) => [
    row.xRaw ?? row.x,
    ...data.series.map((s) => row[s.key]),
  ]);
  return (
    <DataTable
      block={{ type: "table", columns, rows, dataset_id: block.dataset_id }}
    />
  );
}

/** Anything the chart silently did to the data is stated out loud. */
function Caveats({ data }: { data: ChartData }) {
  const notes: string[] = [];
  if (data.droppedValues > 0) {
    notes.push(
      `${formatCompact(data.droppedValues)} non-numeric value${data.droppedValues === 1 ? "" : "s"} omitted`,
    );
  }
  if (data.totalCategories > data.rows.length && data.rows.length > 0) {
    notes.push(
      `showing ${data.rows.length} of ${data.totalCategories} — see the table`,
    );
  }
  if (notes.length === 0) return null;

  return (
    <p
      className={cn(
        "border-t border-border px-4 py-1.5 text-[12px] text-ink-tertiary",
      )}
    >
      {notes.join(" · ")}
    </p>
  );
}
