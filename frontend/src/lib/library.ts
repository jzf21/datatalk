/**
 * The library's shared grammar: runs grouped under day headings, newest first.
 * Used by the sidebar's Recent rail and by the Reports/Dashboards indexes, so a
 * run sits under the same day label in both places.
 */

export interface Dated {
  createdAt: string;
}

export function groupByDay<T extends Dated>(
  items: T[],
  now: Date = new Date(),
): [string, T[]][] {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const label = dayLabel(item.createdAt, now);
    const bucket = groups.get(label);
    if (bucket) bucket.push(item);
    else groups.set(label, [item]);
  }
  return [...groups.entries()];
}

export function dayLabel(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Earlier";
  const days = Math.floor(
    (startOfDay(now) - startOfDay(d)) / (24 * 60 * 60 * 1000),
  );
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/**
 * Every whitespace-separated term must appear somewhere in the title, in any
 * order -- "sla breach" finds "Which accounts breached SLA most often".
 * A `#12` or bare `12` term also matches the run's id.
 */
export function matchesQuery(
  item: { id: number; title: string },
  query: string,
): boolean {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return true;
  const title = item.title.toLowerCase();
  const id = String(item.id);
  return terms.every(
    (term) => title.includes(term) || term.replace(/^#/, "") === id,
  );
}
