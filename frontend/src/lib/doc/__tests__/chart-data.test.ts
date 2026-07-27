import { describe, expect, it } from "vitest";

import {
  formatTooltipValue,
  inferUnit,
  toChartData,
} from "../chart-data";
import type { ChartBlockData } from "@/lib/api/types";

const chart = (over: Partial<ChartBlockData> = {}): ChartBlockData => ({
  type: "chart",
  chart_type: "bar",
  title: "T",
  x: { label: "month", values: ["2026-01-01", "2026-02-01", "2026-03-01"] },
  series: [{ name: "calls", values: [10, 20, 30] }],
  ...over,
});

describe("series keys", () => {
  it("maps to s0..sN and keeps the human name as the label", () => {
    const data = toChartData(
      chart({
        series: [
          { name: "avg resolution (hrs)", values: [1, 2, 3] },
          { name: 'weird."name"', values: [4, 5, 6] },
        ],
      }),
    );
    expect(data.series.map((s) => s.key)).toEqual(["s0", "s1"]);
    expect(data.series[1].label).toBe('weird."name"');
    // Column names would emit invalid CSS custom properties otherwise.
    expect(data.series.every((s) => /^s\d+$/.test(s.key))).toBe(true);
  });
});

describe("numeric coercion", () => {
  it("counts non-numeric values instead of drawing them as a flat line", () => {
    const data = toChartData(
      chart({ series: [{ name: "calls", values: [10, "nope", 30] }] }),
    );
    expect(data.droppedValues).toBe(1);
    expect(data.rows.map((r) => r.s0)).toEqual([10, null, 30]);
  });

  it("coerces stringified Decimals, which ClickHouse sends as strings", () => {
    const data = toChartData(
      chart({ series: [{ name: "cost", values: ["1.50", "2.25", "3.00"] }] }),
    );
    expect(data.rows.map((r) => r.s0)).toEqual([1.5, 2.25, 3]);
    expect(data.droppedValues).toBe(0);
  });
});

describe("unit inference", () => {
  it("treats a precise 0..1 column as a percentage", () => {
    expect(inferUnit("success_rate", [0.9975901089689857, 0.98])).toBe(
      "ratio-percent",
    );
  });

  it("leaves an unnamed low-precision 0..1 column alone", () => {
    // A score of 0.5 is not 50%.
    expect(inferUnit("score", [0.5, 0.8])).toBe("none");
  });

  it("recognises currency, duration and count names", () => {
    expect(inferUnit("revenue_usd", [100, 200])).toBe("currency");
    expect(inferUnit("resolution_hours", [4, 9])).toBe("duration");
    expect(inferUnit("calls", [10, 20])).toBe("count");
  });

  it("always shows the raw value beside a scaled ratio", () => {
    expect(formatTooltipValue(0.9975901089689857, "ratio-percent")).toBe(
      "99.76% (0.9975901089689857)",
    );
  });
});

describe("temporal x-axis", () => {
  it("sorts ascending, because row order is not reading order", () => {
    const data = toChartData(
      chart({
        x: { label: "d", values: ["2026-03-01", "2026-01-01", "2026-02-01"] },
        series: [{ name: "n", values: [3, 1, 2] }],
      }),
    );
    expect(data.temporal).toBe(true);
    expect(data.rows.map((r) => r.s0)).toEqual([1, 2, 3]);
  });
});

describe("form inference", () => {
  it("renders a single point as a value, not a one-bar chart", () => {
    const data = toChartData(
      chart({
        x: { label: "m", values: ["Jan"] },
        series: [{ name: "n", values: [5] }],
      }),
    );
    expect(data.form).toBe("single-value");
  });

  it("turns a pie into a share bar, and a 2-slice pie into a meter", () => {
    expect(toChartData(chart({ chart_type: "pie" })).form).toBe("share-bar");
    expect(
      toChartData(
        chart({
          chart_type: "pie",
          x: { label: "k", values: ["a", "b"] },
          series: [{ name: "n", values: [1, 2] }],
        }),
      ).form,
    ).toBe("meter");
  });

  it("demotes a 3-series area to a line, since nothing says to stack", () => {
    const data = toChartData(
      chart({
        chart_type: "area",
        series: [
          { name: "a", values: [1, 2, 3] },
          { name: "b", values: [1, 2, 3] },
          { name: "c", values: [1, 2, 3] },
        ],
      }),
    );
    expect(data.form).toBe("line");
  });

  it("uses small multiples rather than a dual axis when units conflict", () => {
    const data = toChartData(
      chart({
        series: [
          { name: "revenue_usd", values: [100, 200, 300] },
          { name: "calls", values: [10, 20, 30] },
        ],
      }),
    );
    expect(data.form).toBe("small-multiples");
  });

  it("goes horizontal rather than rotating long labels", () => {
    const data = toChartData(
      chart({
        x: {
          label: "account",
          values: ["a-very-long-account-name", "b", "c"],
        },
      }),
    );
    expect(data.form).toBe("horizontal-bar");
  });

  it("is empty when there are no rows", () => {
    const data = toChartData(
      chart({ x: { label: "m", values: [] }, series: [] }),
    );
    expect(data.form).toBe("empty");
    expect(data.rows).toEqual([]);
  });
});

describe("declared units", () => {
  it("lets an explicit unit beat name inference", () => {
    const data = toChartData(
      chart({ unit: "currency", series: [{ name: "calls", values: [10, 20, 30] }] }),
    );
    expect(data.series[0].unit).toBe("currency");
  });

  it("maps a declared ratio to scaled percent display", () => {
    const data = toChartData(
      chart({ unit: "ratio", series: [{ name: "score", values: [0.5, 0.8, 0.9] }] }),
    );
    expect(data.series[0].unit).toBe("ratio-percent");
  });

  it("does not scale an asserted ratio that is not one", () => {
    // 45 displayed as 4500% would be worse than no unit at all.
    const data = toChartData(
      chart({ unit: "ratio", series: [{ name: "score", values: [0.5, 45, 0.9] }] }),
    );
    expect(data.series[0].unit).toBe("percent");
  });

  it("suppresses the mixed-units small-multiples demotion", () => {
    const data = toChartData(
      chart({
        unit: "count",
        series: [
          { name: "revenue_usd", values: [100, 200, 300] },
          { name: "calls", values: [10, 20, 30] },
        ],
      }),
    );
    expect(data.form).toBe("bar");
  });

  it("falls back to inference on an unknown declared unit", () => {
    const data = toChartData(
      // A future backend could ship a unit this bundle doesn't know.
      chart({ unit: "furlongs" as never }),
    );
    expect(data.series[0].unit).toBe("count");
  });
});

describe("model form hints", () => {
  it("honors horizontal_bar for categorical data", () => {
    const data = toChartData(
      chart({
        chart_type: "horizontal_bar",
        x: { label: "account", values: ["a", "b", "c"] },
      }),
    );
    expect(data.form).toBe("horizontal-bar");
  });

  it("refuses horizontal_bar on a time axis", () => {
    const data = toChartData(chart({ chart_type: "horizontal_bar" }));
    expect(data.temporal).toBe(true);
    expect(data.form).toBe("bar");
  });

  it("falls through an unknown chart_type to bar", () => {
    // Pins the degrade path an old cached bundle relies on for new documents.
    const data = toChartData(chart({ chart_type: "sunburst" as never }));
    expect(data.form).toBe("bar");
  });

  it("keeps a stacked 3-series area as an area", () => {
    const data = toChartData(
      chart({
        chart_type: "area",
        stacked: true,
        series: [
          { name: "a", values: [1, 2, 3] },
          { name: "b", values: [1, 2, 3] },
          { name: "c", values: [1, 2, 3] },
        ],
      }),
    );
    expect(data.form).toBe("area");
    expect(data.stacked).toBe(true);
  });

  it("still demotes stacking when units conflict", () => {
    const data = toChartData(
      chart({
        stacked: true,
        series: [
          { name: "revenue_usd", values: [100, 200, 300] },
          { name: "calls", values: [10, 20, 30] },
        ],
      }),
    );
    expect(data.form).toBe("small-multiples");
    expect(data.stacked).toBe(false);
  });

  it("never stacks a single series", () => {
    const data = toChartData(chart({ stacked: true }));
    expect(data.stacked).toBe(false);
  });
});

describe("category cap", () => {
  it("keeps the biggest 25 and reports the true total", () => {
    const n = 43;
    const data = toChartData(
      chart({
        x: {
          label: "k",
          values: Array.from({ length: n }, (_, i) => `cat${i}`),
        },
        series: [{ name: "v", values: Array.from({ length: n }, (_, i) => i) }],
      }),
    );
    expect(data.totalCategories).toBe(n);
    expect(data.rows.length).toBe(25);
    // The largest value must survive the cap.
    expect(data.rows.some((r) => r.s0 === n - 1)).toBe(true);
  });
});
