/**
 * Dashboard filter state: presets, normalization, and URL encoding.
 *
 * Pure on purpose. `vitest` runs with `environment: "node"` and the project has
 * no jsdom and no `@testing-library`, so the only way this logic gets tested is
 * if it lives outside a component -- the same discipline that keeps
 * `chart-data.ts`, `widths.ts` and `scope.ts` testable. Every time-dependent
 * function takes `now` rather than reading the clock, for the same reason.
 */

import type { FilterDef, FilterValue, FilterValues } from "@/lib/api/types";

export interface DatePreset {
  id: string;
  label: string;
  /** null = no lower bound ("All time"). */
  days: number | null;
}

export const DATE_PRESETS: DatePreset[] = [
  { id: "last_24_hours", label: "Last 24 hours", days: 1 },
  { id: "last_7_days", label: "Last 7 days", days: 7 },
  { id: "last_30_days", label: "Last 30 days", days: 30 },
  { id: "last_90_days", label: "Last 90 days", days: 90 },
  { id: "last_12_months", label: "Last 12 months", days: 365 },
  { id: "all_time", label: "All time", days: null },
];

const PRESET_IDS = new Set(DATE_PRESETS.map((p) => p.id));

/** `YYYY-MM-DD` in UTC. The server re-resolves presets anyway; this is for display. */
function isoDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

export function isDateValue(
  v: FilterValue | null | undefined,
): v is { preset?: string; from?: string; to?: string } {
  return !!v && ("preset" in v || "from" in v || "to" in v);
}

export function isDimensionValue(
  v: FilterValue | null | undefined,
): v is { all?: boolean; values?: string[] } {
  return !!v && ("all" in v || "values" in v);
}

/**
 * Resolve a date filter to a concrete window, for labelling only.
 *
 * The server resolves presets itself against its own clock and never trusts a
 * client-computed window, so this is display, not authority.
 */
export function resolveRange(
  value: FilterValue | null | undefined,
  now: number,
): { from: string | null; to: string | null } {
  if (!isDateValue(value)) return { from: null, to: null };
  if (value.preset && value.preset !== "custom") {
    const preset = DATE_PRESETS.find((p) => p.id === value.preset);
    if (!preset) return { from: null, to: null };
    if (preset.days === null) return { from: null, to: null };
    return { from: isoDay(now - preset.days * 86_400_000), to: isoDay(now) };
  }
  const from = value.from || null;
  const to = value.to || null;
  // A backwards custom range is a typo, not a filter. Swap rather than send it.
  if (from && to && from > to) return { from: to, to: from };
  return { from, to };
}

export function defaultValues(defs: FilterDef[]): FilterValues {
  const out: FilterValues = {};
  for (const def of defs) {
    if (def.default) {
      out[def.id] = def.default;
    } else if (def.kind === "dimension") {
      out[def.id] = { all: true };
    }
  }
  return out;
}

/**
 * Drop what the dashboard cannot honour, and put what remains in a canonical form.
 *
 * The sorting is not cosmetic: it is what makes `filterSignature` stable, and
 * therefore what stops `?f.region=A&f.region=B` and `?f.region=B&f.region=A`
 * from becoming two cache entries and two identical warehouse round trips.
 */
export function normalizeValues(
  defs: FilterDef[],
  values: FilterValues,
): FilterValues {
  const byId = new Map(defs.map((d) => [d.id, d]));
  const out: FilterValues = {};

  for (const [id, raw] of Object.entries(values)) {
    const def = byId.get(id);
    if (!def || !raw) continue;

    if (def.kind === "date_range") {
      if (!isDateValue(raw)) continue;
      const next: { preset?: string; from?: string; to?: string } = {};
      if (raw.preset && (PRESET_IDS.has(raw.preset) || raw.preset === "custom")) {
        next.preset = raw.preset;
      }
      if (raw.from) next.from = raw.from;
      if (raw.to) next.to = raw.to;
      if (next.from && next.to && next.from > next.to) {
        [next.from, next.to] = [next.to, next.from];
      }
      if (Object.keys(next).length) out[id] = next;
      continue;
    }

    if (!isDimensionValue(raw)) continue;
    if (raw.all) {
      // "All" is a flag, never the full option list: binding every option would
      // silently exclude anything the probe truncated or anything added since.
      out[id] = { all: true };
      continue;
    }
    const allowed = def.options ? new Set(def.options) : null;
    const picked = Array.from(
      new Set((raw.values ?? []).filter((v) => !allowed || allowed.has(v))),
    ).sort();
    // An empty selection means "no constraint", which is what "all" already
    // says -- collapsing them keeps one cache entry instead of two.
    out[id] = picked.length ? { values: picked } : { all: true };
  }

  return out;
}

/** A stable cache key for one filter combination. */
export function filterSignature(values: FilterValues): string {
  const keys = Object.keys(values).sort();
  if (!keys.length) return "";
  return keys
    .map((k) => {
      const v = values[k];
      if (isDimensionValue(v)) {
        if (v.all) return `${k}=*`;
        return `${k}=${[...(v.values ?? [])].sort().join("")}`;
      }
      if (isDateValue(v)) {
        return `${k}=${v.preset ?? ""}|${v.from ?? ""}|${v.to ?? ""}`;
      }
      return `${k}=`;
    })
    .join("");
}

export function isDefault(defs: FilterDef[], values: FilterValues): boolean {
  return (
    filterSignature(normalizeValues(defs, values)) ===
    filterSignature(normalizeValues(defs, defaultValues(defs)))
  );
}

// --- URL encoding ------------------------------------------------------------
//
// Repeated params rather than a comma-joined list: warehouse dimension values
// routinely contain commas, and a joined scheme needs a second escaping layer
// that will eventually be got wrong.

const DIM_PREFIX = "f.";

export function encodeFilters(
  defs: FilterDef[],
  values: FilterValues,
): URLSearchParams {
  const params = new URLSearchParams();
  const normalized = normalizeValues(defs, values);
  const defaults = normalizeValues(defs, defaultValues(defs));

  for (const def of defs) {
    const v = normalized[def.id];
    if (!v) continue;
    // An absent key means "the default", so the canonical view has a clean URL.
    if (filterSignature({ x: v }) === filterSignature({ x: defaults[def.id] })) {
      continue;
    }
    if (def.kind === "date_range" && isDateValue(v)) {
      if (v.preset === "custom" && v.from && v.to) {
        params.set(def.id, `${v.from}..${v.to}`);
      } else if (v.preset) {
        params.set(def.id, v.preset);
      }
    } else if (isDimensionValue(v) && v.values) {
      for (const one of v.values) params.append(`${DIM_PREFIX}${def.id}`, one);
    }
  }
  return params;
}

export function decodeFilters(
  defs: FilterDef[],
  params: URLSearchParams | null,
): FilterValues {
  const out = defaultValues(defs);
  if (!params) return normalizeValues(defs, out);

  for (const def of defs) {
    if (def.kind === "date_range") {
      const raw = params.get(def.id);
      if (!raw) continue;
      if (raw.includes("..")) {
        const [from, to] = raw.split("..");
        out[def.id] = { preset: "custom", from, to };
      } else {
        out[def.id] = { preset: raw };
      }
      continue;
    }
    const picked = params.getAll(`${DIM_PREFIX}${def.id}`);
    if (picked.length) out[def.id] = { values: picked };
  }
  return normalizeValues(defs, out);
}
