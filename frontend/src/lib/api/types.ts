/**
 * Wire types, hand-mirrored from `datatalk/agent/blocks.py`.
 *
 * Two things to know before editing:
 *
 * 1. `block_to_dict` DROPS every key whose value is `None`, so almost every
 *    field here is optional even when the Python dataclass declares it.
 * 2. The streaming events are not in the OpenAPI schema, so these cannot be
 *    generated from it. Keep this file in sync with `blocks.py` by hand.
 */

// --- blocks ----------------------------------------------------------------

export type ChartType = "bar" | "horizontal_bar" | "line" | "area" | "pie";

/** Presentation hint: how every series of a chart is formatted. `ratio` means
 *  the values are 0–1 fractions to display as percentages. */
export type ChartUnit = "percent" | "ratio" | "currency" | "duration" | "count";

/** Which delta direction is an improvement. Absent means `up_is_good`. */
export type StatDirection = "up_is_good" | "down_is_good" | "neutral";

/** Present on materialized blocks so a figure can cite the query behind it. */
interface Cited {
  dataset_id?: string;
  /** 1..12, clamped server-side. Only meaningful inside a `row`. */
  width?: number;
}

export interface HeadingBlock extends Cited {
  type: "heading";
  text: string;
  /** Defaults to 2; clamped 1..3 by the renderer. */
  level?: number;
}

export interface ParagraphBlock extends Cited {
  type: "paragraph";
  text: string;
}

export interface TableBlock extends Cited {
  type: "table";
  columns: string[];
  rows: CellValue[][];
}

export interface ChartSeries {
  name: string;
  values: CellValue[];
}

export interface ChartBlockData extends Cited {
  type: "chart";
  chart_type: ChartType;
  /** Always present after materialization, but may be an empty string. */
  title: string;
  /** Model-declared unit shared by every series; overrides name inference. */
  unit?: ChartUnit;
  /** Series are parts of a whole (bar/area only). */
  stacked?: boolean;
  x: { label: string; values: CellValue[] };
  series: ChartSeries[];
}

export interface StatBlock extends Cited {
  type: "stat";
  label: string;
  unit?: string;
  direction?: StatDirection;
  value?: CellValue;
  delta?: number;
  /** null when the prior value was 0 or either value was non-numeric. */
  delta_pct?: number | null;
}

export interface RowBlock {
  type: "row";
  width?: number;
  children: Block[];
}

/** ClickHouse rows reach us as JSON, with datetimes/Decimals serialized to strings. */
export type CellValue = string | number | boolean | null;

export type Block =
  | HeadingBlock
  | ParagraphBlock
  | TableBlock
  | ChartBlockData
  | StatBlock
  | RowBlock;

export interface BlockDocument {
  blocks: Block[];
}

// --- provenance ------------------------------------------------------------

export interface CapturedQuery {
  /** "q1", "q2", ... -- globally unique across a report and all its Q&A turns. */
  dataset_id: string;
  sql: string;
  row_count: number;
  columns: string[];
  /** Which of the org's sources this ran against. Absent on pre-multi-source rows. */
  source?: string | null;
}

// --- plain JSON endpoints --------------------------------------------------

export interface SourceHealth {
  name: string;
  type: string;
  is_default: boolean;
  ok: boolean;
  version?: string;
  database?: string;
  table_count?: number;
  error?: string;
}

export interface HealthResponse {
  /** One entry per configured data source; a dead one is a state, not an error. */
  sources: SourceHealth[];
  openai: { ok: boolean; model?: string; reply?: string; error?: string };
}

export interface SchemaResponse {
  schema_context: string;
}

export interface ReportSummary {
  id: number;
  request: string;
  created_at: string;
}

export interface QATurn {
  id: number;
  question: string;
  /** Note the wire name: the store calls this `answer_document`. */
  document: BlockDocument;
  queries: CapturedQuery[];
  created_at: string;
}

export interface ReportDetail extends ReportSummary {
  /** Flattened plain text, derived from the document. */
  markdown: string;
  document: BlockDocument;
  queries: CapturedQuery[];
  qa_turns: QATurn[];
}

export interface DashboardSummary {
  id: number;
  request: string;
  title: string;
  created_at: string;
}

/** One structured finding from the insight pass (all fields model-written,
 * so every one is optional in practice). */
export interface Insight {
  dataset_id?: string;
  kind?: string;
  finding?: string;
  importance?: number;
  presentation_hint?: string;
}

/** The insight pass's full output: findings plus which datasets should lead
 * the grid, which were dropped, and what the data could not answer. */
export interface InsightSet {
  insights?: Insight[];
  lead?: string[];
  drop?: string[];
  gaps?: string[];
}

export interface DashboardDetail extends DashboardSummary {
  document: BlockDocument;
  queries: CapturedQuery[];
  /** Structured findings from the insight pass; {} for pre-insight saves. */
  insights: InsightSet;
  /** Markdown, or null when it has never been analyzed. */
  analysis: string | null;
  /**
   * Whether a refresh can replay this dashboard exactly. False for dashboards
   * saved before the authoring document was kept: those still refresh, but
   * their stat tiles stay frozen because the column each one read was never
   * recorded (see agent/blocks.py `dematerialize`).
   */
  refreshable: boolean;
  /** Per-dashboard filter definitions; {} when none are configured. */
  filters: DashboardFilters;
}

/** Mirrors dashboards.filters in Postgres. Empty until filters are configured. */
export interface DashboardFilters {
  version?: number;
  filters?: FilterDef[];
}

export interface FilterDef {
  id: string;
  kind: "date_range" | "dimension";
  label: string;
  /** dimension only */
  column?: string;
  source?: string;
  multi?: boolean;
  options?: string[];
  /** True when the option probe hit its cap -- a UI badge, not a correctness issue. */
  options_truncated?: boolean;
  default?: FilterValue | null;
}

export type FilterValue =
  | { preset?: string; from?: string; to?: string }
  | { all?: boolean; values?: string[] };

/** Selections keyed by filter id, as sent to /refresh. */
export type FilterValues = Record<string, FilterValue>;

/** What the client sends to define a filter; options and templates are derived. */
export interface FilterDefInput {
  id: string;
  kind: "date_range" | "dimension";
  label: string;
  column?: string;
  source?: string;
  multi?: boolean;
}

export interface FilterConfigResponse {
  filters: DashboardFilters;
  /** Datasets the rewrite reached. */
  wired: string[];
  /** Datasets it could not, with the reason -- surfaced, never hidden. */
  skipped: { dataset_id: string; reason: string }[];
}

/** One captured query's fate during a refresh. */
export interface DatasetStatus {
  dataset_id: string;
  source: string | null;
  status: "ok" | "unavailable" | "rejected" | "error" | "timeout";
  ok: boolean;
  reason?: string;
  message?: string;
  row_count?: number;
  truncated?: boolean;
  filtered?: boolean;
}

/** The body of POST /api/dashboards/{id}/refresh. */
export interface DashboardData {
  dashboard_id: number;
  refreshed_at: string;
  document: BlockDocument;
  datasets: DatasetStatus[];
  /** Some query failed, or some stat tile could not be rebuilt. */
  partial: boolean;
  /** False when replayed from a de-materialized document (legacy dashboard). */
  exact: boolean;
  frozen_stats: number;
  /** Datasets no filter could be bound to, so their numbers ignore the selection. */
  unfiltered: string[];
  applied_filters: FilterValues;
}

export interface Suggestion {
  id: number;
  text: string;
  created_at: string;
}

export interface AnalyzeResponse {
  /** Block-level markdown. */
  analysis: string;
  source: "own" | "external";
}

// --- NDJSON stream events --------------------------------------------------

/** Every line is `{"kind": ..., "data": {...}}`. */
export type RunEvent =
  | { kind: "memory"; data: { count: number; suggestions: string[] } }
  // step/max_steps arrive on the analyst loop's turn announcements. A step is
  // one assistant *turn*, which under batching runs several queries.
  | { kind: "status"; data: { message: string; step?: number; max_steps?: number } }
  | { kind: "plan"; data: { sections: PlanSection[] } }
  // `query_id` correlates sql/result/error within a turn: batched queries run
  // concurrently on the server, so "most recent running step" is ambiguous.
  | { kind: "sql"; data: { sql: string; query_id?: string; source?: string | null } }
  | {
      kind: "result";
      data: {
        dataset_id: string;
        row_count: number;
        columns: string[];
        query_id?: string;
        source?: string;
      };
    }
  // Used for BOTH recoverable retries and fatal worker crashes. See isFatal().
  | { kind: "error"; data: { message: string; query_id?: string } }
  // Heartbeat while a long LLM call is in flight; carries nothing.
  | { kind: "ping"; data: Record<string, never> }
  // Dashboard runs only: the insight pass's structured findings.
  | { kind: "insights"; data: InsightSet }
  | { kind: "report"; data: { document: BlockDocument } }
  | { kind: "dashboard"; data: { document: BlockDocument } }
  | {
      kind: "answer";
      data: {
        turn_id: number;
        document: BlockDocument;
        queries: CapturedQuery[];
      };
    }
  | {
      kind: "saved";
      data: {
        report_id?: number;
        dashboard_id?: number;
        queries: CapturedQuery[];
        steps: number;
      };
    }
  | { kind: "done"; data: Record<string, never> };

export type RunEventKind = RunEvent["kind"];

export interface PlanSection {
  id: string;
  title: string;
  goal: string;
  data_questions: string[];
}
