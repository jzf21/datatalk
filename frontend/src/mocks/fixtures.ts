import type {
  BlockDocument,
  CapturedQuery,
  PlanSection,
} from "@/lib/api/types";

/** Deterministic pseudo-data -- no Math.random, so fixtures diff cleanly. */
const MONTHS = [
  "2025-10-01",
  "2025-11-01",
  "2025-12-01",
  "2026-01-01",
  "2026-02-01",
  "2026-03-01",
];

export const FIXTURE_QUERIES: CapturedQuery[] = [
  {
    dataset_id: "q1",
    sql: "SELECT toStartOfMonth(created_at) AS month, count() AS tickets\nFROM jira.issues\nWHERE created_at >= now() - INTERVAL 6 MONTH\nGROUP BY month ORDER BY month",
    row_count: 6,
    columns: ["month", "tickets"],
  },
  {
    dataset_id: "q2",
    sql: "SELECT account, count() AS tickets, avg(resolution_hours) AS avg_hours, countIf(breached)/count() AS sla_rate\nFROM jira.issues GROUP BY account ORDER BY tickets DESC LIMIT 10",
    row_count: 10,
    columns: ["account", "tickets", "avg_hours", "sla_rate"],
  },
  {
    dataset_id: "q3",
    sql: "SELECT priority, count() AS n FROM jira.issues GROUP BY priority",
    row_count: 4,
    columns: ["priority", "n"],
  },
  {
    dataset_id: "q4",
    sql: "SELECT account, tickets - lag_tickets AS mom_change, avg_hours - lag_hours AS hours_change\nFROM monthly_by_account WHERE month = toStartOfMonth(now()) ORDER BY mom_change DESC",
    row_count: 6,
    columns: ["account", "mom_change", "hours_change"],
  },
];

export const FIXTURE_PLAN: PlanSection[] = [
  {
    id: "s1",
    title: "Volume by month",
    goal: "Establish the six-month baseline before looking at anything else.",
    data_questions: ["Total tickets per month", "Tickets by account per month"],
  },
  {
    id: "s2",
    title: "Resolution time",
    goal: "Find where time is actually going.",
    data_questions: ["Average resolution hours by account"],
  },
  {
    id: "s3",
    title: "SLA breach rate",
    goal: "Quantify how often we miss, and for whom.",
    data_questions: ["Breach rate by account", "Breach rate trend"],
  },
];

export const FIXTURE_REPORT: BlockDocument = {
  blocks: [
    { type: "heading", text: "Ticket resolution trends", level: 1 },
    {
      type: "paragraph",
      text: "Ticket volume rose **38%** over the last six months while average resolution time held roughly flat. The load is concentrated in three accounts.",
    },
    {
      type: "chart",
      chart_type: "line",
      title: "Tickets created by month",
      dataset_id: "q1",
      x: { label: "month", values: MONTHS },
      series: [{ name: "tickets", values: [1420, 1533, 1490, 1712, 1858, 1961] }],
    },
    { type: "heading", text: "Where the load sits", level: 2 },
    {
      type: "table",
      dataset_id: "q2",
      columns: ["account", "tickets", "avg_hours", "sla_rate"],
      rows: [
        ["Northwind Traders", 2890, 18.42, 0.9975901089689857],
        ["Contoso", 1560, 22.15, 0.9712],
        ["Fabrikam", 1204, 15.03, 0.9944],
        ["Adventure Works", 980, 31.8, 0.9218],
        ["Tailspin Toys", 742, 12.5, 0.9989],
        ["Wide World Importers", 610, 27.44, 0.9401],
        ["Litware", 455, 19.9, 0.9655],
        ["Proseware", 388, 24.1, null],
      ],
    },
    { type: "heading", text: "Month-over-month movement", level: 2 },
    {
      // A signed table: mixed-sign numeric columns wear the credit/debit pills.
      type: "table",
      dataset_id: "q4",
      columns: ["account", "mom_change", "hours_change"],
      rows: [
        ["Northwind Traders", 312, -2.4],
        ["Contoso", 148, 1.8],
        ["Fabrikam", 0, -0.6],
        ["Adventure Works", -87, 4.1],
        ["Tailspin Toys", -12, 0],
        ["Wide World Importers", -203, -3.25],
      ],
    },
    {
      type: "chart",
      chart_type: "bar",
      title: "Average resolution time by account",
      dataset_id: "q2",
      x: {
        label: "account",
        values: [
          "Northwind Traders",
          "Contoso",
          "Fabrikam",
          "Adventure Works",
          "Tailspin Toys",
        ],
      },
      series: [{ name: "avg_hours", values: [18.42, 22.15, 15.03, 31.8, 12.5] }],
    },
    {
      type: "chart",
      chart_type: "pie",
      title: "Tickets by priority",
      dataset_id: "q3",
      x: { label: "priority", values: ["Low", "Medium", "High", "Critical"] },
      series: [{ name: "n", values: [4210, 3180, 1502, 340] }],
    },
    {
      type: "paragraph",
      text: "_chart unavailable: unknown column 'sla_target'_",
    },
  ],
};

export const FIXTURE_DASHBOARD: BlockDocument = {
  blocks: [
    {
      type: "row",
      children: [
        {
          type: "stat",
          label: "Total tickets",
          dataset_id: "q1",
          value: 9544,
          width: 3,
          delta: 1103,
          delta_pct: 13.06,
        },
        {
          type: "stat",
          label: "SLA compliance",
          dataset_id: "q2",
          value: 99.76,
          unit: "%",
          width: 3,
          delta: -0.31,
          delta_pct: -0.31,
        },
        {
          type: "stat",
          label: "Avg resolution",
          dataset_id: "q2",
          value: 21.4,
          unit: "h",
          width: 3,
        },
        {
          type: "stat",
          label: "Accounts at risk",
          dataset_id: "q2",
          value: 3,
          width: 3,
        },
      ],
    },
    {
      type: "row",
      children: [
        {
          type: "chart",
          chart_type: "area",
          title: "Tickets by month",
          dataset_id: "q1",
          width: 7,
          x: { label: "month", values: MONTHS },
          series: [
            { name: "tickets", values: [1420, 1533, 1490, 1712, 1858, 1961] },
          ],
        },
        {
          type: "chart",
          chart_type: "bar",
          title: "By priority",
          dataset_id: "q3",
          width: 5,
          x: { label: "priority", values: ["Low", "Med", "High", "Crit"] },
          series: [{ name: "n", values: [4210, 3180, 1502, 340] }],
        },
      ],
    },
    {
      type: "row",
      children: [
        {
          type: "table",
          dataset_id: "q2",
          width: 12,
          columns: ["account", "tickets", "avg_hours", "sla_rate"],
          rows: [
            ["Northwind Traders", 2890, 18.42, 0.9975901089689857],
            ["Contoso", 1560, 22.15, 0.9712],
            ["Fabrikam", 1204, 15.03, 0.9944],
          ],
        },
      ],
    },
  ],
};

/** A deliberately awkward document: every degenerate case in one place. */
export const FIXTURE_EDGE_CASES: BlockDocument = {
  blocks: [
    { type: "heading", text: "Edge cases", level: 2 },
    {
      type: "chart",
      chart_type: "bar",
      title: "Single point",
      dataset_id: "q1",
      x: { label: "month", values: ["2026-03-01"] },
      series: [{ name: "tickets", values: [1961] }],
    },
    {
      type: "chart",
      chart_type: "bar",
      title: "Conflicting units (must become small multiples)",
      dataset_id: "q2",
      x: { label: "month", values: MONTHS },
      series: [
        { name: "revenue_usd", values: [12000, 14500, 13900, 16200, 17800, 19100] },
        { name: "calls", values: [140, 152, 149, 171, 185, 196] },
      ],
    },
    {
      type: "chart",
      chart_type: "area",
      title: "Three areas (must become a line chart)",
      dataset_id: "q1",
      x: { label: "month", values: MONTHS },
      series: [
        { name: "a_count", values: [10, 20, 30, 40, 50, 60] },
        { name: "b_count", values: [15, 18, 24, 33, 41, 52] },
        { name: "c_count", values: [5, 9, 14, 19, 27, 31] },
      ],
    },
    {
      type: "chart",
      chart_type: "bar",
      title: "Non-numeric values present",
      dataset_id: "q3",
      x: { label: "k", values: ["a", "b", "c", "d"] },
      series: [{ name: "n", values: [10, "n/a", 30, 40] }],
    },
    {
      type: "chart",
      chart_type: "bar",
      title: "No rows",
      dataset_id: "q3",
      x: { label: "k", values: [] },
      series: [],
    },
    {
      type: "row",
      children: [
        { type: "stat", label: "One", dataset_id: "q1", value: 1 },
        { type: "stat", label: "Of", dataset_id: "q1", value: 2 },
        { type: "stat", label: "Five", dataset_id: "q1", value: 3 },
        { type: "stat", label: "Even", dataset_id: "q1", value: 4 },
        { type: "stat", label: "Split", dataset_id: "q1", value: 5 },
      ],
    },
  ],
};
