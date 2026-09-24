"use client";

import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { toast } from "sonner";

import {
  syncConnection,
  type ConnectionPublic,
  type SyncState,
} from "@/lib/api/auth";
import { ApiError, apiErrorMessage } from "@/lib/api/client";
import { syncSummary } from "@/lib/connections/sync";
import { Button } from "@/components/ui/button";

/**
 * Freshness line plus Sync / Full resync for one synced (Jira) source.
 *
 * The stream is consumed here rather than lifted into the page: progress is
 * only interesting while it is running, and the page only needs to know when
 * it finished (`onSynced`) so it can refetch the list and the schema.
 */
export function SyncControls({
  orgId,
  connection,
  canEdit,
  autoStart = false,
  onSynced,
}: {
  orgId: string;
  connection: ConnectionPublic;
  canEdit: boolean;
  /** Start an initial sync on mount -- set right after the source is added. */
  autoStart?: boolean;
  onSynced: () => void;
}) {
  const [running, setRunning] = useState<null | { full: boolean; issues: number }>(
    null,
  );
  // The state the stream reported last, so the line updates before the list
  // refetch lands.
  const [latest, setLatest] = useState<SyncState | null>(null);
  const abort = useRef<AbortController | null>(null);

  // Whichever is newer: the stream's report beats a list that has not refetched
  // yet, but must not shadow a later sync (cron, another tab) forever.
  const stored = connection.sync ?? null;
  const state =
    latest &&
    (!stored?.last_synced_at ||
      (latest.last_synced_at ?? "") >= stored.last_synced_at)
      ? latest
      : stored;
  const summary = running
    ? {
        tone: "muted" as const,
        text: `${running.full ? "Full resync" : "Syncing"}… ${running.issues.toLocaleString()} issues so far`,
      }
    : syncSummary(state);

  const run = async (full: boolean) => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setRunning({ full, issues: 0 });
    try {
      for await (const ev of syncConnection(orgId, connection.id, full, controller.signal)) {
        if (ev.kind === "page") {
          setRunning({ full, issues: ev.data.issues });
        } else if (ev.kind === "synced") {
          setLatest(ev.data);
          toast.success(`Synced ${connection.name}`);
        } else if (ev.kind === "error") {
          if (ev.data.code === "sync_in_progress") {
            toast.info(apiErrorMessage("sync_in_progress"));
          } else {
            toast.error(`Sync failed: ${ev.data.message}`);
          }
        }
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      toast.error(err instanceof ApiError ? err.message : "Couldn't reach the API.");
    } finally {
      if (abort.current === controller) abort.current = null;
      setRunning(null);
      onSynced();
    }
  };

  const started = useRef(false);
  useEffect(() => {
    if (autoStart && canEdit && !started.current) {
      started.current = true;
      void run(false);
    }
    // Once per mount; `run` is recreated every render and must not retrigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStart, canEdit]);

  // Leaving the page abandons the stream (the server finishes the sync anyway).
  useEffect(() => () => abort.current?.abort(), []);

  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
      <p
        role="status"
        className={`text-[12px] ${
          summary.tone === "bad"
            ? "text-destructive"
            : summary.tone === "good"
              ? "text-ink-secondary"
              : "text-ink-tertiary"
        }`}
      >
        {summary.text}
      </p>
      {canEdit && (
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            disabled={running !== null}
            onClick={() => run(false)}
          >
            <RefreshCw
              className={`size-3.5 ${running ? "animate-spin" : ""}`}
              aria-hidden
            />
            Sync now
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={running !== null}
            onClick={() => run(true)}
            title="Re-read everything. The only way issues deleted in Jira disappear here."
          >
            Full resync
          </Button>
        </div>
      )}
    </div>
  );
}
