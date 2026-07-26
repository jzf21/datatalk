"use client";

import { ChevronRight, Database } from "lucide-react";

import { DATA_SOURCES, type DataSource } from "@/lib/connections/sources";

/**
 * Step one of the connection flow: pick what kind of warehouse this is.
 *
 * The list is driven by `DATA_SOURCES`, so a new warehouse type shows up here
 * without touching this component.
 */
export function SourcePicker({
  disabled,
  onSelect,
}: {
  disabled?: boolean;
  onSelect: (source: DataSource) => void;
}) {
  return (
    <div className="space-y-2" role="list">
      {DATA_SOURCES.map((source) => (
        <button
          key={source.id}
          type="button"
          role="listitem"
          disabled={disabled}
          onClick={() => onSelect(source)}
          className="flex w-full items-center gap-3 rounded-[6px] border border-border bg-card p-3 text-left transition-colors duration-[120ms] hover:border-ink-tertiary hover:bg-accent/60 disabled:pointer-events-none disabled:opacity-60"
        >
          <Database
            className="size-4 shrink-0 text-ink-tertiary"
            aria-hidden
          />
          <span className="min-w-0 flex-1">
            <span className="block text-[14px] font-medium text-ink-primary">
              {source.name}
            </span>
            <span className="block text-[12px] text-ink-tertiary">
              {source.blurb}
            </span>
          </span>
          <ChevronRight
            className="size-4 shrink-0 text-ink-tertiary"
            aria-hidden
          />
        </button>
      ))}
    </div>
  );
}
