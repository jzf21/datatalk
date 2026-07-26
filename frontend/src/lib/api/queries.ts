"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import * as api from "./endpoints";

/** One place to spell every key, so invalidation can never drift from fetching. */
export const qk = {
  health: ["health"] as const,
  schema: ["schema"] as const,
  reports: ["reports"] as const,
  report: (id: number) => ["report", id] as const,
  dashboards: ["dashboards"] as const,
  dashboard: (id: number) => ["dashboard", id] as const,
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

export function useDashboard(id: number | null) {
  return useQuery({
    queryKey: qk.dashboard(id ?? -1),
    queryFn: () => api.getDashboard(id!),
    enabled: id !== null && Number.isInteger(id),
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
