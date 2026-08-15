"use client";

import { Check, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { REFRESH_OPTIONS, labelFor } from "@/hooks/use-auto-refresh";
import { cn } from "@/lib/utils";

/**
 * The header cluster: how often to re-run, and a way to do it now.
 *
 * A DropdownMenu rather than a ToggleGroup for the interval -- five options in a
 * 56px header would crowd a title that already truncates, and ToggleGroup is
 * established here for switching between two or three *views* (chart/table), not
 * for picking a setting.
 */
export function RefreshControls({
  intervalMs,
  onIntervalChange,
  onRefresh,
  isFetching,
  disabled = false,
}: {
  intervalMs: number | null;
  onIntervalChange: (ms: number | null) => void;
  onRefresh: () => void;
  isFetching: boolean;
  disabled?: boolean;
}) {
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="sm" disabled={disabled}>
            Auto: {labelFor(intervalMs)}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-40">
          <DropdownMenuLabel className="label-caps text-ink-tertiary">
            Auto-refresh
          </DropdownMenuLabel>
          {REFRESH_OPTIONS.map((option) => (
            <DropdownMenuItem
              key={option.label}
              onSelect={() => onIntervalChange(option.ms)}
              className="justify-between"
            >
              {option.label}
              {option.ms === intervalMs && <Check className="size-3.5" />}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      <Button
        variant="outline"
        size="sm"
        // An explicit refetch cancels and restarts an in-flight one, so leaving
        // this live during a fetch would let a impatient click restart the same
        // warehouse query repeatedly.
        disabled={disabled || isFetching}
        onClick={onRefresh}
      >
        <RefreshCw className={cn("size-3.5", isFetching && "animate-spin")} />
        {isFetching ? "Refreshing…" : "Refresh"}
      </Button>
    </>
  );
}
