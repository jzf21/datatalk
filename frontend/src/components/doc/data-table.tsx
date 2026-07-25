"use client";

import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, MoreHorizontal } from "lucide-react";
import { toast } from "sonner";

import type { CellValue, TableBlock } from "@/lib/api/types";
import { formatCount, toNumber } from "@/lib/format";
import {
  formatCell,
  inferColumns,
  isNumericColumn,
  rawCellTitle,
} from "@/lib/doc/columns";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TickChip } from "./tick-chip";

const INITIAL_ROWS = 200;

type SortState = { index: number; dir: "asc" | "desc" } | null;

export function DataTable({ block }: { block: TableBlock }) {
  const [sort, setSort] = useState<SortState>(null);
  const [showAll, setShowAll] = useState(false);

  const columns = block.columns ?? [];
  const allRows = block.rows ?? [];
  const metas = useMemo(
    () => inferColumns(columns, allRows),
    [columns, allRows],
  );

  const sorted = useMemo(() => {
    if (!sort) return allRows;
    const meta = metas[sort.index];
    const factor = sort.dir === "asc" ? 1 : -1;
    return [...allRows].sort(
      (a, b) => factor * compare(a[sort.index], b[sort.index], meta?.kind),
    );
  }, [allRows, sort, metas]);

  const rows = showAll ? sorted : sorted.slice(0, INITIAL_ROWS);
  const truncated = sorted.length > rows.length;

  if (columns.length === 0 || allRows.length === 0) {
    return (
      <figure className="rounded-[6px] border border-border bg-card p-6 text-center">
        <p className="text-[13px] text-ink-secondary">No rows returned.</p>
        <TickChip datasetId={block.dataset_id} className="mt-2 inline-block" />
      </figure>
    );
  }

  return (
    <figure className="rounded-[6px] border border-border bg-card">
      <figcaption className="flex items-center justify-between gap-3 px-3 py-2">
        <span className="cite text-ink-tertiary">
          {formatCount(allRows.length, "row")} ·{" "}
          {formatCount(columns.length, "column")}
        </span>
        <span className="flex items-center gap-1">
          <TickChip datasetId={block.dataset_id} />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="size-6"
                aria-label="Table actions"
              >
                <MoreHorizontal className="size-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onSelect={() => copy(toCsv(columns, sorted), "Copied as CSV")}
              >
                Copy as CSV
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() =>
                  copy(toMarkdown(columns, sorted), "Copied as Markdown")
                }
              >
                Copy as Markdown
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </span>
      </figcaption>

      <div
        className={cn(
          "overflow-x-auto border-t border-border",
          // Only trap a scroll once the table is actually long.
          allRows.length > 20 && "max-h-[560px] overflow-y-auto",
        )}
      >
        <table className="w-full border-collapse text-[13px]">
          <caption className="sr-only">
            {formatCount(allRows.length, "row")} of query results
          </caption>
          <thead className="sticky top-0 z-10 bg-muted">
            <tr>
              {columns.map((name, i) => {
                const meta = metas[i];
                const numeric = isNumericColumn(meta);
                const sorting = sort?.index === i ? sort.dir : undefined;
                return (
                  <th
                    key={name + i}
                    scope="col"
                    aria-sort={
                      sorting === "asc"
                        ? "ascending"
                        : sorting === "desc"
                          ? "descending"
                          : "none"
                    }
                    className={cn(
                      "label-caps whitespace-nowrap border-b border-border-strong px-3 py-2 text-ink-secondary",
                      // Headers adopt their cells' alignment.
                      numeric ? "text-right" : "text-left",
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => setSort(nextSort(sort, i))}
                      className={cn(
                        "inline-flex items-center gap-1 hover:text-ink-primary",
                        numeric && "flex-row-reverse",
                      )}
                    >
                      {name}
                      {/* Reserved space, so headers never shift on sort. */}
                      <span className="inline-block w-3">
                        {sorting === "asc" && <ArrowUp className="size-3" />}
                        {sorting === "desc" && <ArrowDown className="size-3" />}
                      </span>
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, r) => (
              <tr
                key={r}
                className={cn(
                  "border-b border-border last:border-b-0 hover:bg-accent/50",
                  // The greenbar nod: so quiet you feel it rather than see it.
                  allRows.length > 12 && r % 2 === 1 && "bg-background/40",
                )}
              >
                {columns.map((name, c) => {
                  const meta = metas[c];
                  return (
                    <td
                      key={name + c}
                      title={rawCellTitle(row[c], meta)}
                      className={cn(
                        "px-3 py-1.5 text-ink-primary",
                        isNumericColumn(meta)
                          ? "text-right tabular-nums"
                          : "text-left",
                        row[c] === null && "text-ink-disabled",
                      )}
                    >
                      {formatCell(row[c], meta)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {truncated && (
        <div className="border-t border-border px-3 py-2 text-[12px] text-ink-secondary">
          Showing {rows.length.toLocaleString()} of{" "}
          {sorted.length.toLocaleString()} ·{" "}
          <button
            type="button"
            onClick={() => setShowAll(true)}
            className="text-link underline underline-offset-2"
          >
            Load all
          </button>
        </div>
      )}
    </figure>
  );
}

function nextSort(current: SortState, index: number): SortState {
  if (current?.index !== index) return { index, dir: "desc" };
  if (current.dir === "desc") return { index, dir: "asc" };
  return null;
}

function compare(a: CellValue, b: CellValue, kind?: string): number {
  if (a === null || a === "") return 1; // nulls sink, in both directions
  if (b === null || b === "") return -1;
  if (kind === "number" || kind === "datetime") {
    const na = kind === "number" ? toNumber(a) : Date.parse(String(a));
    const nb = kind === "number" ? toNumber(b) : Date.parse(String(b));
    if (na !== null && nb !== null && !Number.isNaN(na) && !Number.isNaN(nb)) {
      return na - nb;
    }
  }
  return String(a).localeCompare(String(b), undefined, { numeric: true });
}

function toCsv(columns: string[], rows: CellValue[][]): string {
  const escape = (v: CellValue) => {
    const s = v === null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [
    columns.map(escape).join(","),
    ...rows.map((row) => row.map(escape).join(",")),
  ].join("\n");
}

function toMarkdown(columns: string[], rows: CellValue[][]): string {
  const cell = (v: CellValue) => String(v ?? "").replace(/\|/g, "\\|");
  return [
    `| ${columns.map(cell).join(" | ")} |`,
    `| ${columns.map(() => "---").join(" | ")} |`,
    ...rows.map((row) => `| ${row.map(cell).join(" | ")} |`),
  ].join("\n");
}

async function copy(text: string, message: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(message);
  } catch {
    toast.error("Couldn't copy to the clipboard.");
  }
}
