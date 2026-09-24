import type { SyncState } from "@/lib/api/auth";
import { formatCount, formatRelativeTime } from "@/lib/format";

/**
 * One line saying how fresh a synced source is.
 *
 * Freshness is the one thing a synced source gives up against a live one, so
 * it is stated plainly wherever the source is listed -- a chart over Jira data
 * is only as current as this line says.
 */
export function syncSummary(
  state: SyncState | null | undefined,
  now: number = Date.now(),
): { tone: "muted" | "good" | "bad"; text: string } {
  if (!state || state.status === "never") {
    return { tone: "muted", text: "Not synced yet" };
  }
  if (state.status === "running") {
    return { tone: "muted", text: "Syncing…" };
  }
  const when = state.last_synced_at
    ? formatRelativeTime(state.last_synced_at, now)
    : null;
  if (state.status === "error") {
    const prefix = when ? `Last good sync ${when}. ` : "";
    return {
      tone: "bad",
      text: `${prefix}Last attempt failed: ${state.error ?? "unknown error"}`,
    };
  }
  const issues = state.stats?.tables?.issues;
  return {
    tone: "good",
    text: [
      `Synced ${when ?? "recently"}`,
      issues !== undefined ? formatCount(issues, "issue") : null,
    ]
      .filter(Boolean)
      .join(" · "),
  };
}
