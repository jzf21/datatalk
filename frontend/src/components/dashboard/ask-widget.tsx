"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { streamAIWidget } from "@/lib/api/endpoints";
import { qk } from "@/lib/api/queries";

/**
 * "Ask for a widget": the agents write one more query and its blocks.
 *
 * Unlike a catalog widget, its query is model-written and has no filter
 * template, so it is honest about ignoring the dashboard's filters -- the
 * refresh names it in `unfiltered`, and this panel says so before it runs.
 */
export function AskWidget({
  dashboardId,
  hasFilters,
  onAdded,
}: {
  dashboardId: number;
  hasFilters: boolean;
  onAdded: () => void;
}) {
  const qc = useQueryClient();
  const [request, setRequest] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => () => abort.current?.abort(), []);

  const run = async () => {
    const ctrl = new AbortController();
    abort.current = ctrl;
    setRunning(true);
    setError(null);
    setStatus("Starting…");
    let added = false;
    try {
      for await (const event of streamAIWidget(
        dashboardId,
        request.trim(),
        ctrl.signal,
      )) {
        if (event.kind === "status") setStatus(event.data.message);
        else if (event.kind === "sql") setStatus("Querying…");
        else if (event.kind === "error") setError(event.data.message);
        else if (event.kind === "widget") added = true;
      }
    } catch (err) {
      if (!ctrl.signal.aborted) setError(String(err));
    } finally {
      setRunning(false);
      setStatus(null);
    }
    if (added) {
      await qc.invalidateQueries({ queryKey: qk.dashboard(dashboardId) });
      await qc.invalidateQueries({
        queryKey: qk.dashboardDataAll(dashboardId),
      });
      setRequest("");
      onAdded();
    }
  };

  return (
    <div className="space-y-2 border-t border-border px-4 pt-4">
      <p className="text-[13px] font-medium text-ink-primary">
        Ask for a widget
      </p>
      <Textarea
        aria-label="Describe the widget"
        placeholder="e.g. Bugs opened per week by component"
        value={request}
        maxLength={2000}
        rows={3}
        onChange={(e) => setRequest(e.target.value)}
        disabled={running}
      />
      {hasFilters && (
        <p className="text-[12px] text-ink-tertiary">
          A generated widget writes its own query, so the dashboard&apos;s
          filters won&apos;t apply to it. The dashboard will say so.
        </p>
      )}
      {status && (
        <p className="text-[12px] text-ink-secondary" aria-live="polite">
          {status}
        </p>
      )}
      {error && (
        <p
          role="alert"
          className="border-l-2 border-destructive pl-2 text-[12px] text-ink-secondary"
        >
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        {running && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => abort.current?.abort()}
          >
            Stop
          </Button>
        )}
        <Button size="sm" onClick={run} disabled={running || !request.trim()}>
          {running ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Sparkles className="size-3.5" />
          )}
          Generate
        </Button>
      </div>
    </div>
  );
}
