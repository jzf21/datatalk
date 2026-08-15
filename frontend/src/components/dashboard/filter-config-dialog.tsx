"use client";

import { Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useUpdateDashboardFilters } from "@/lib/api/queries";
import type {
  CapturedQuery,
  FilterDef,
  FilterDefInput,
} from "@/lib/api/types";

/** `Region` -> `region`; the server requires ^[a-z][a-z0-9_]{0,31}$. */
function toId(label: string, taken: Set<string>): string {
  const base =
    label
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^[^a-z]+/, "")
      .replace(/_+$/, "")
      .slice(0, 32) || "filter";
  let id = base;
  let n = 2;
  while (taken.has(id)) id = `${base}_${n++}`.slice(0, 32);
  return id;
}

/**
 * Define which filters a dashboard has.
 *
 * A Dialog because this is a save-or-cancel form, and `dialog.tsx` was installed
 * and used nowhere. It is the first dashboard-editing UI in the app, so it is
 * kept to exactly this: naming filters and picking their columns. Everything
 * else -- the option lists, the query rewrites, and whether a rewrite is
 * trustworthy -- is derived server-side, because only the server can verify a
 * rewrite by running it.
 */
export function FilterConfigDialog({
  dashboardId,
  open,
  onOpenChange,
  existing,
  queries,
}: {
  dashboardId: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  existing: FilterDef[];
  queries: CapturedQuery[];
}) {
  const [drafts, setDrafts] = useState<FilterDefInput[]>(() =>
    existing.map((d) => ({
      id: d.id,
      kind: d.kind,
      label: d.label,
      column: d.column,
      source: d.source,
      multi: d.multi ?? true,
    })),
  );
  const update = useUpdateDashboardFilters(dashboardId);

  // Every column the dashboard's own queries returned. No new endpoint needed:
  // this is already on the client, and it is exactly the set a filter can
  // plausibly attach to.
  const columns = useMemo(() => {
    const seen = new Set<string>();
    for (const q of queries) for (const c of q.columns ?? []) seen.add(c);
    return [...seen].sort();
  }, [queries]);

  const sources = useMemo(() => {
    const seen = new Set<string>();
    for (const q of queries) if (q.source) seen.add(q.source);
    return [...seen].sort();
  }, [queries]);

  const add = (kind: FilterDefInput["kind"]) => {
    const taken = new Set(drafts.map((d) => d.id));
    const label = kind === "date_range" ? "Period" : "Dimension";
    setDrafts([
      ...drafts,
      {
        id: toId(label, taken),
        kind,
        label,
        multi: true,
        ...(kind === "dimension"
          ? { column: columns[0], source: sources[0] }
          : {}),
      },
    ]);
  };

  const patch = (index: number, next: Partial<FilterDefInput>) =>
    setDrafts(drafts.map((d, i) => (i === index ? { ...d, ...next } : d)));

  const remove = (index: number) =>
    setDrafts(drafts.filter((_, i) => i !== index));

  const save = () =>
    update.mutate(drafts, { onSuccess: () => onOpenChange(false) });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Dashboard filters</DialogTitle>
          <DialogDescription>
            Saving rewrites this dashboard&rsquo;s queries so the filters apply
            before each total is computed, then checks each rewrite by running
            it. That takes a few seconds.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {drafts.length === 0 && (
            <p className="text-[13px] text-ink-secondary">
              No filters yet. Add a date range to scope every number to a period,
              or a dimension to break the dashboard down by a column.
            </p>
          )}

          {drafts.map((draft, i) => (
            <div
              key={draft.id}
              className="flex items-end gap-2 rounded-[4px] border border-border p-3"
            >
              <div className="flex-1 space-y-1">
                <Label className="label-caps text-ink-tertiary">
                  {draft.kind === "date_range" ? "Date range" : "Dimension"}
                </Label>
                <Input
                  aria-label="Filter label"
                  value={draft.label}
                  onChange={(e) => patch(i, { label: e.target.value })}
                />
              </div>

              {draft.kind === "dimension" && (
                <div className="flex-1 space-y-1">
                  <Label className="label-caps text-ink-tertiary">Column</Label>
                  <select
                    aria-label="Filter column"
                    className="h-8 w-full rounded-[4px] border border-border bg-transparent px-2 text-[13px]"
                    value={draft.column ?? ""}
                    onChange={(e) => patch(i, { column: e.target.value })}
                  >
                    {columns.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              <Button
                variant="ghost"
                size="sm"
                aria-label={`Remove ${draft.label}`}
                onClick={() => remove(i)}
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          ))}

          {!update.isPending && (
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => add("date_range")}>
                <Plus className="size-3.5" />
                Date range
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={!columns.length}
                onClick={() => add("dimension")}
              >
                <Plus className="size-3.5" />
                Dimension
              </Button>
            </div>
          )}

          {update.isPending && (
            <div className="space-y-2" aria-live="polite">
              <p className="text-[12px] text-ink-tertiary">
                Rewriting and verifying each query&hellip;
              </p>
              {[0, 1].map((i) => (
                <Skeleton key={i} className="h-3 w-full" />
              ))}
            </div>
          )}

          {update.isError && (
            <p
              role="alert"
              className="border-l-2 border-destructive py-1 pl-3 text-[13px] text-ink-secondary"
            >
              {String(update.error)}
            </p>
          )}

          {/*
            A filter that reached only some widgets has to say so here, while the
            person who set it up is still looking -- not weeks later when a
            number fails to move.
          */}
          {update.data && update.data.skipped.length > 0 && (
            <div className="text-[12px] text-ink-tertiary">
              <p>
                Applied to {update.data.wired.length} of{" "}
                {update.data.wired.length + update.data.skipped.length} queries.
                These could not be filtered:
              </p>
              <ul className="mt-1 space-y-0.5">
                {update.data.skipped.map((s) => (
                  <li key={s.dataset_id}>
                    <span className="font-mono">{s.dataset_id}</span> —{" "}
                    {s.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={update.isPending}
          >
            Cancel
          </Button>
          <Button size="sm" onClick={save} disabled={update.isPending}>
            {update.isPending ? "Saving…" : "Save filters"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
