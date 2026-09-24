import { describe, expect, it } from "vitest";

import type { SyncState } from "@/lib/api/auth";
import { syncSummary } from "@/lib/connections/sync";

const NOW = Date.parse("2024-03-10T12:00:00Z");

function state(over: Partial<SyncState>): SyncState {
  return { status: "ok", last_synced_at: null, error: null, stats: {}, ...over };
}

describe("syncSummary", () => {
  it("says so when a source has never synced", () => {
    expect(syncSummary(null, NOW)).toEqual({ tone: "muted", text: "Not synced yet" });
    expect(syncSummary(state({ status: "never" }), NOW).text).toBe("Not synced yet");
  });

  it("states freshness and size after a good sync", () => {
    const s = state({
      last_synced_at: "2024-03-10T11:55:00Z",
      stats: { tables: { issues: 1234 } },
    });
    expect(syncSummary(s, NOW)).toEqual({
      tone: "good",
      text: "Synced 5 min ago · 1,234 issues",
    });
  });

  it("keeps the last good sync visible when the latest attempt failed", () => {
    // The data is still there and still this old; the failure must not hide that.
    const s = state({
      status: "error",
      last_synced_at: "2024-03-10T10:00:00Z",
      error: "Jira rejected the email / API token (401).",
    });
    const out = syncSummary(s, NOW);
    expect(out.tone).toBe("bad");
    expect(out.text).toContain("Last good sync 2 h ago");
    expect(out.text).toContain("401");
  });

  it("does not invent a last good sync that never happened", () => {
    const out = syncSummary(state({ status: "error", error: "boom" }), NOW);
    expect(out.text).toBe("Last attempt failed: boom");
  });
});
