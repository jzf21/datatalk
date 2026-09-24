"use client";

import { Filter, Loader2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ApiError, apiErrorMessage } from "@/lib/api/client";
import { useSetWidgetFilters } from "@/lib/api/queries";
import type { DashboardFilters, FilterValues } from "@/lib/api/types";
import { isDimensionValue, supportedFilters } from "@/lib/dashboards/filters";

/**
 * Which dashboard filters reach one widget's query, and any value pinned for it.
 *
 * Applies immediately, separately from the layout's Save: wiring lives with the
 * query, not the arrangement, and it is shared by every block over the same
 * dataset -- which the panel says, so a change to one tile surprises nobody.
 */
export function WidgetFilters({
  dashboardId,
  datasetId,
  filters,
  sharedWith,
}: {
  dashboardId: number;
  datasetId: string;
  filters: DashboardFilters;
  /** How many other tiles read the same dataset. */
  sharedWith: number;
}) {
  const template = filters.templates?.[datasetId];
  const defs = filters.filters ?? [];
  const supported = supportedFilters(template);
  const [open, setOpen] = useState(false);
  const save = useSetWidgetFilters(dashboardId);

  const initialWired = template?.filters ?? [...supported];
  const [wired, setWired] = useState<Set<string>>(new Set(initialWired));
  const [overrides, setOverrides] = useState<FilterValues>(
    template?.overrides ?? {},
  );

  if (!template || !supported.size) {
    return (
      <Button
        variant="ghost"
        size="icon-sm"
        disabled
        title="This widget's query takes no dashboard filters"
        aria-label="No filters for this widget"
      >
        <Filter className="size-3.5" />
      </Button>
    );
  }

  const pinnedValue = (id: string) => {
    const v = overrides[id];
    return isDimensionValue(v) && v.values?.length ? v.values[0] : "";
  };

  const apply = () => {
    save.mutate(
      { datasetId, wired: [...wired], overrides },
      {
        onSuccess: () => setOpen(false),
        onError: (err) =>
          toast.error(
            err instanceof ApiError
              ? apiErrorMessage(err.message)
              : "Couldn't reach the API.",
          ),
      },
    );
  };

  const customised =
    initialWired.length < supported.size ||
    Object.keys(template.overrides ?? {}).length > 0;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Widget filters"
          title={
            customised ? "Filters customised for this widget" : "Widget filters"
          }
          className={customised ? "text-primary-text" : undefined}
        >
          <Filter className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 space-y-3">
        <div>
          <p className="text-[13px] font-medium text-ink-primary">
            Filters for this widget
          </p>
          <p className="text-[12px] text-ink-tertiary">
            Unticked filters don&apos;t apply here. A pinned value holds
            whatever the dashboard is set to.
            {sharedWith > 0 &&
              ` Also applies to ${sharedWith} other tile${sharedWith === 1 ? "" : "s"} on the same query.`}
          </p>
        </div>

        <ul className="space-y-2">
          {defs.map((def) => {
            const can = supported.has(def.id);
            const on = can && wired.has(def.id);
            const options = def.options ?? [];
            return (
              <li key={def.id} className="space-y-1.5">
                <div className="flex items-center gap-2">
                  <Checkbox
                    id={`wire-${datasetId}-${def.id}`}
                    checked={on}
                    disabled={!can}
                    onCheckedChange={(checked) => {
                      const next = new Set(wired);
                      if (checked) next.add(def.id);
                      else next.delete(def.id);
                      setWired(next);
                    }}
                  />
                  <Label
                    htmlFor={`wire-${datasetId}-${def.id}`}
                    className={
                      can ? "text-[13px]" : "text-[13px] text-ink-disabled"
                    }
                  >
                    {def.label}
                    {!can && " — not available for this query"}
                  </Label>
                </div>
                {on && def.kind === "dimension" && options.length > 0 && (
                  <select
                    aria-label={`Pin ${def.label}`}
                    className="ml-6 h-7 w-[calc(100%-1.5rem)] rounded-[4px] border border-border bg-transparent px-2 text-[12px]"
                    value={pinnedValue(def.id)}
                    onChange={(e) => {
                      const next = { ...overrides };
                      if (e.target.value)
                        next[def.id] = { values: [e.target.value] };
                      else delete next[def.id];
                      setOverrides(next);
                    }}
                  >
                    <option value="">Follow the dashboard</option>
                    {options.map((o) => (
                      <option key={o} value={o}>
                        Always: {def.option_labels?.[o] ?? o}
                      </option>
                    ))}
                  </select>
                )}
              </li>
            );
          })}
        </ul>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button size="sm" onClick={apply} disabled={save.isPending}>
            {save.isPending && <Loader2 className="size-3.5 animate-spin" />}
            Apply
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
