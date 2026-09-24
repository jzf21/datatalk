"use client";

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { ApiError } from "./client";
import * as api from "./endpoints";
import { filterSignature } from "@/lib/dashboards/filters";
import type { FilterDefInput, FilterValues } from "./types";

/** One place to spell every key, so invalidation can never drift from fetching. */
export const qk = {
  health: ["health"] as const,
  schema: ["schema"] as const,
  reports: ["reports"] as const,
  report: (id: number) => ["report", id] as const,
  dashboards: ["dashboards"] as const,
  dashboard: (id: number) => ["dashboard", id] as const,
  dashboardTemplates: ["dashboard-templates"] as const,
  // A separate root from `dashboard`, deliberately. Nested under it, the
  // useAnalyzeDashboard invalidation below would prefix-match and discard every
  // cached materialization -- re-running warehouse SQL because someone clicked
  // Analyze. `dashboardDataAll` is the prefix for "every filter combination".
  dashboardData: (id: number, sig: string) =>
    ["dashboard-data", id, sig] as const,
  dashboardDataAll: (id: number) => ["dashboard-data", id] as const,
  suggestions: ["suggestions"] as const,
  context: (orgId: string | null) => ["context", orgId] as const,
  contextFile: (orgId: string | null, path: string) =>
    ["context", orgId, path] as const,
};

export function useHealth() {
  return useQuery({
    queryKey: qk.health,
    queryFn: ({ signal }) => api.getHealth(signal),
    refetchInterval: 30_000,
    // The endpoint never 500s -- failure is reported inside the payload, so a
    // retry would only be masking an unreachable server.
    retry: false,
  });
}

export function useReports() {
  return useQuery({ queryKey: qk.reports, queryFn: api.listReports });
}

export function useReport(id: number | null) {
  return useQuery({
    queryKey: qk.report(id ?? -1),
    queryFn: () => api.getReport(id!),
    enabled: id !== null && Number.isInteger(id),
  });
}

export function useDashboards() {
  return useQuery({ queryKey: qk.dashboards, queryFn: api.listDashboards });
}

export function useDashboardTemplates() {
  return useQuery({
    queryKey: qk.dashboardTemplates,
    queryFn: api.listDashboardTemplates,
    // Changes only when a source is added or removed.
    staleTime: 5 * 60_000,
  });
}

export function useCreateDashboardFromTemplate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.createDashboardFromTemplate,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.dashboards });
    },
  });
}

export function useDashboard(id: number | null) {
  return useQuery({
    queryKey: qk.dashboard(id ?? -1),
    queryFn: () => api.getDashboard(id!),
    enabled: id !== null && Number.isInteger(id),
  });
}

/**
 * A dashboard's *live* document: its captured queries, re-run now.
 *
 * A query rather than a mutation on a timer, because this is server state keyed
 * by (dashboard, filter values) -- which is exactly what TanStack caches. As a
 * mutation we would hand-roll the interval, the in-flight guard, the tab-hidden
 * pause, the per-filter cache and previous-data retention; all five come free
 * here, and query-core already skips an interval tick while the tab is hidden
 * and dedupes a tick against an in-flight fetch, so polls cannot stack.
 */
export function useDashboardData(
  id: number | null,
  filters: FilterValues,
  refreshMs: number | null,
  enabled = true,
) {
  const signature = filterSignature(filters);
  return useQuery({
    queryKey: qk.dashboardData(id ?? -1, signature),
    queryFn: async ({ signal }) => {
      try {
        return await api.refreshDashboard(id!, filters, signal);
      } catch (err) {
        // Another tab (or a double-click) holds the per-dashboard guard. That
        // is not an error worth showing: the numbers on screen are still the
        // ones we have, and the next tick will get through.
        // `message` carries the raw machine code -- see announceApiError, which
        // compares it the same way. Wording is applied only at display time.
        if (err instanceof ApiError && err.message === "refresh_in_progress") {
          return null;
        }
        throw err;
      }
    },
    enabled: enabled && id !== null && Number.isInteger(id),
    // Deliberately not the app-wide 30s: this number is about warehouse cost.
    // Long enough that arriving straight from the generator does not re-run SQL
    // that just ran, short enough that any real revisit refetches -- which is
    // the whole bug being fixed.
    staleTime: 15_000,
    gcTime: 5 * 60_000,
    // Changing a filter must never blank the grid.
    placeholderData: keepPreviousData,
    // Function form, so the clock restarts after each fetch *finishes*.
    refetchInterval: (query) =>
      refreshMs && query.state.fetchStatus === "idle" ? refreshMs : false,
    // Overrides the app-wide false, but only while polling: returning to a live
    // dashboard should catch up immediately, a parked one should stay quiet.
    refetchOnWindowFocus: refreshMs !== null,
    // A refresh runs real warehouse SQL; an automatic retry doubles the load on
    // exactly the failure it is meant to survive.
    retry: 0,
  });
}

export function useUpdateDashboardFilters(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (filters: FilterDefInput[]) =>
      api.updateDashboardFilters(id, filters),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.dashboard(id) });
      // Not invalidate: changed definitions mean every cached materialization is
      // the answer to a question this dashboard no longer asks, so they are
      // dropped outright rather than refetched.
      qc.removeQueries({ queryKey: qk.dashboardDataAll(id) });
    },
  });
}

export function useSuggestions() {
  return useQuery({ queryKey: qk.suggestions, queryFn: api.listSuggestions });
}

export function useAddSuggestion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.addSuggestion,
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.suggestions }),
  });
}

export function useDeleteSuggestion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.deleteSuggestion,
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.suggestions }),
  });
}

export function useAnalyze() {
  return useMutation({ mutationFn: api.analyze });
}

export function useAnalyzeDashboard(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (focus?: string) => api.analyzeDashboard(id, focus),
    // The analysis is persisted server-side, so the detail query is now stale.
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.dashboard(id) }),
  });
}

/**
 * Every structural edit invalidates the same things: the stored dashboard, and
 * every cached materialization -- those are answers laid out for a grid that no
 * longer exists. Invalidated rather than removed, so the view on screen
 * refetches in its new shape (keeping the old one visible meanwhile) and the
 * other filter combinations refetch only if they are visited again.
 */
function useDashboardEdit<V>(id: number, fn: (vars: V) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: qk.dashboard(id) });
      await qc.invalidateQueries({ queryKey: qk.dashboardDataAll(id) });
    },
  });
}

export function useSaveDashboardLayout(id: number) {
  return useDashboardEdit(id, (doc: unknown) => api.saveDashboardLayout(id, doc));
}

export function useAddDashboardWidget(id: number) {
  return useDashboardEdit(id, (widgetKey: string) => api.addDashboardWidget(id, widgetKey));
}

export function useSetWidgetFilters(id: number) {
  return useDashboardEdit(
    id,
    (vars: { datasetId: string; wired?: string[]; overrides: FilterValues }) =>
      api.setWidgetFilters(id, vars.datasetId, { wired: vars.wired, overrides: vars.overrides }),
  );
}
