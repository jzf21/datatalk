"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useDashboards, useReports } from "@/lib/api/queries";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

interface Run {
  key: string;
  href: string;
  id: number;
  kind: "RP" | "DB";
  title: string;
  createdAt: string;
}

/**
 * Recent runs as ledger lines, grouped by day.
 *
 * The archive *is* the navigation: the old UI hid past reports behind a
 * `<select>` with no address-bar trace, so nothing was ever linkable.
 */
export function LibraryRail() {
  const pathname = usePathname();
  const reports = useReports();
  const dashboards = useDashboards();

  const loading = reports.isPending || dashboards.isPending;
  const failed = reports.isError && dashboards.isError;

  const runs: Run[] = [
    ...(reports.data ?? []).map((r) => ({
      key: `r${r.id}`,
      href: `/reports/${r.id}`,
      id: r.id,
      kind: "RP" as const,
      title: r.request,
      createdAt: r.created_at,
    })),
    ...(dashboards.data ?? []).map((d) => ({
      key: `d${d.id}`,
      href: `/dashboards/${d.id}`,
      id: d.id,
      kind: "DB" as const,
      title: d.title || d.request,
      createdAt: d.created_at,
    })),
  ].sort((a, b) => b.createdAt.localeCompare(a.createdAt));

  return (
    <div className="mt-6 min-h-0 flex-1 overflow-y-auto px-2 pb-4">
      <h2 className="label-caps px-2 pb-2 text-ink-tertiary">Recent</h2>

      {loading && (
        <ul className="space-y-2 px-2 pt-1" aria-hidden>
          {[0, 1, 2].map((i) => (
            <li key={i}>
              <Skeleton className="h-3 w-full" />
            </li>
          ))}
        </ul>
      )}

      {!loading && failed && (
        <p className="px-2 text-[12px] text-ink-tertiary">
          Couldn&rsquo;t load runs.
        </p>
      )}

      {!loading && !failed && runs.length === 0 && (
        <p className="px-2 text-[12px] text-ink-tertiary">
          Runs you generate appear here.
        </p>
      )}

      <ul className="space-y-px">
        {groupByDay(runs).map(([day, items]) => (
          <li key={day}>
            <h3 className="label-caps px-2 pb-1 pt-3 text-ink-disabled">{day}</h3>
            <ul className="space-y-px">
              {items.map((run) => {
                const active = pathname === run.href;
                return (
                  <li key={run.key}>
                    <Link
                      href={run.href}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "flex items-baseline gap-2 rounded-[4px] px-2 py-1 text-[13px] transition-colors duration-[120ms]",
                        active
                          ? "bg-accent text-ink-primary"
                          : "text-ink-secondary hover:bg-accent/60 hover:text-ink-primary",
                      )}
                    >
                      <span className="cite shrink-0 text-ink-tertiary">
                        {run.kind}
                      </span>
                      <span className="truncate">{run.title}</span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}

function groupByDay(runs: Run[]): [string, Run[]][] {
  const groups = new Map<string, Run[]>();
  for (const run of runs) {
    const label = dayLabel(run.createdAt);
    const bucket = groups.get(label);
    if (bucket) bucket.push(run);
    else groups.set(label, [run]);
  }
  return [...groups.entries()];
}

function dayLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Earlier";
  const today = new Date();
  const days = Math.floor(
    (startOfDay(today) - startOfDay(d)) / (24 * 60 * 60 * 1000),
  );
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}
