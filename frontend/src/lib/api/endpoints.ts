import { apiDelete, apiGet, apiPost, apiPut } from "./client";
import { streamNdjson } from "./stream";
import type {
  AnalyzeResponse,
  DashboardData,
  DashboardDetail,
  DashboardSummary,
  FilterConfigResponse,
  FilterDefInput,
  FilterValues,
  HealthResponse,
  ReportDetail,
  ReportSummary,
  SchemaResponse,
  Suggestion,
} from "./types";

// --- connection checkout ---------------------------------------------------

export const getHealth = (signal?: AbortSignal) =>
  apiGet<HealthResponse>("/api/health", signal);

export const getSchema = (refresh = false) =>
  apiGet<SchemaResponse>(`/api/schema?refresh=${refresh}`);

// --- reports ---------------------------------------------------------------

export const listReports = () =>
  apiGet<{ reports: ReportSummary[] }>("/api/reports").then((r) => r.reports);

export const getReport = (id: number) =>
  apiGet<ReportDetail>(`/api/reports/${id}`);

export const streamReport = (
  request: string,
  useMemory: boolean,
  signal?: AbortSignal,
) => streamNdjson("/api/report", { request, use_memory: useMemory }, signal);

export const streamAsk = (
  reportId: number,
  question: string,
  signal?: AbortSignal,
) => streamNdjson(`/api/reports/${reportId}/ask`, { question }, signal);

// --- dashboards ------------------------------------------------------------

export const listDashboards = () =>
  apiGet<{ dashboards: DashboardSummary[] }>("/api/dashboards").then(
    (r) => r.dashboards,
  );

export const getDashboard = (id: number) =>
  apiGet<DashboardDetail>(`/api/dashboards/${id}`);

export const streamDashboard = (
  request: string,
  useMemory: boolean,
  signal?: AbortSignal,
) => streamNdjson("/api/dashboard", { request, use_memory: useMemory }, signal);

export const analyzeDashboard = (
  id: number,
  focus?: string,
  useMemory = true,
) =>
  apiPost<{ analysis: string }>(`/api/dashboards/${id}/analyze`, {
    focus: focus || null,
    use_memory: useMemory,
  });

/**
 * Re-run a dashboard's captured queries and return a freshly materialized
 * document. A POST because it spends warehouse time and carries a body, but
 * semantically a read: it never writes the stored snapshot back.
 */
export const refreshDashboard = (
  id: number,
  filters: FilterValues,
  signal?: AbortSignal,
) => apiPost<DashboardData>(`/api/dashboards/${id}/refresh`, { filters }, signal);

/**
 * Define a dashboard's filters. The server probes each dimension's values and
 * rewrites the captured queries to accept the filters, verifying each rewrite by
 * running it -- so this is slow (an LLM call plus N queries) and deliberate.
 */
export const updateDashboardFilters = (id: number, filters: FilterDefInput[]) =>
  apiPut<FilterConfigResponse>(`/api/dashboards/${id}/filters`, { filters });

// --- analyze ---------------------------------------------------------------

export const analyze = (input: {
  report_id?: number;
  text?: string;
  focus?: string;
  use_memory?: boolean;
}) =>
  apiPost<AnalyzeResponse>("/api/analyze", {
    report_id: input.report_id ?? null,
    text: input.text ?? null,
    focus: input.focus || null,
    use_memory: input.use_memory ?? true,
  });

// --- house rules (memory) --------------------------------------------------

export const listSuggestions = () =>
  apiGet<{ suggestions: Suggestion[] }>("/api/suggestions").then(
    (r) => r.suggestions,
  );

export const addSuggestion = (text: string) =>
  apiPost<Suggestion>("/api/feedback", { text });

export const deleteSuggestion = (id: number) =>
  apiDelete<{ deleted: number }>(`/api/suggestions/${id}`);
