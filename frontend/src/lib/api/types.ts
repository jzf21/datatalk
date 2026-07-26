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

export type ChartType = "bar" | "line" | "area" | "pie";

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
  x: { label: string; values: CellValue[] };
  series: ChartSeries[];
}

export interface StatBlock extends Cited {
  type: "stat";
  label: string;
  unit?: string;
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

export interface DashboardDetail extends DashboardSummary {
  document: BlockDocument;
  queries: CapturedQuery[];
  /** Markdown, or null when it has never been analyzed. */
  analysis: string | null;
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
  | { kind: "status"; data: { message: string } }
  | { kind: "plan"; data: { sections: PlanSection[] } }
  | { kind: "sql"; data: { sql: string } }
  | {
      kind: "result";
      data: { dataset_id: string; row_count: number; columns: string[] };
    }
  // Used for BOTH recoverable retries and fatal worker crashes. See isFatal().
  | { kind: "error"; data: { message: string } }
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
