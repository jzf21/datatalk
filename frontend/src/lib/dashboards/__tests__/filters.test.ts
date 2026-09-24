import { describe, expect, it } from "vitest";

import type { FilterDef, FilterValues } from "@/lib/api/types";
import {
  DATE_PRESETS,
  decodeFilters,
  defaultValues,
  describeSprint,
  encodeFilters,
  filterSignature,
  isDefault,
  normalizeValues,
  resolveRange,
  supportedFilters,
} from "../filters";

// 2026-08-15T00:00:00Z -- fixed, so nothing here reads the clock.
const NOW = Date.UTC(2026, 7, 15);

const DEFS: FilterDef[] = [
  {
    id: "range",
    kind: "date_range",
    label: "Period",
    default: { preset: "last_30_days" },
  },
  {
    id: "region",
    kind: "dimension",
    label: "Region",
    column: "region",
    multi: true,
    options: ["APAC", "EMEA", "US", "O'Brien, Ltd & Co", "Ünïcode"],
  },
];

describe("resolveRange", () => {
  it("resolves a preset to a concrete window", () => {
    expect(resolveRange({ preset: "last_7_days" }, NOW)).toEqual({
      from: "2026-08-08",
      to: "2026-08-15",
    });
  });

  it("treats all_time as unbounded", () => {
    expect(resolveRange({ preset: "all_time" }, NOW)).toEqual({
      from: null,
      to: null,
    });
  });

  it("swaps a backwards custom range rather than passing it on", () => {
    expect(
      resolveRange({ preset: "custom", from: "2026-03-01", to: "2026-01-01" }, NOW),
    ).toEqual({ from: "2026-01-01", to: "2026-03-01" });
  });

  it("returns nothing for an unknown preset", () => {
    expect(resolveRange({ preset: "last_fortnight" }, NOW)).toEqual({
      from: null,
      to: null,
    });
  });

  it("covers every declared preset without throwing", () => {
    for (const p of DATE_PRESETS) {
      expect(() => resolveRange({ preset: p.id }, NOW)).not.toThrow();
    }
  });
});

describe("filterSignature", () => {
  it("is invariant under key order", () => {
    const a: FilterValues = { region: { values: ["EMEA"] }, range: { preset: "x" } };
    const b: FilterValues = { range: { preset: "x" }, region: { values: ["EMEA"] } };
    expect(filterSignature(a)).toBe(filterSignature(b));
  });

  it("is invariant under dimension value order", () => {
    // The property that keeps one filter combination to one cache entry. If it
    // breaks, the app silently keeps two and refreshes the warehouse twice.
    const a = normalizeValues(DEFS, { region: { values: ["EMEA", "APAC"] } });
    const b = normalizeValues(DEFS, { region: { values: ["APAC", "EMEA"] } });
    expect(filterSignature(a)).toBe(filterSignature(b));
  });

  it("distinguishes genuinely different selections", () => {
    const a = normalizeValues(DEFS, { region: { values: ["EMEA"] } });
    const b = normalizeValues(DEFS, { region: { values: ["US"] } });
    expect(filterSignature(a)).not.toBe(filterSignature(b));
  });

  it("gives the empty selection a stable empty key", () => {
    expect(filterSignature({})).toBe("");
  });
});

describe("normalizeValues", () => {
  it("drops a filter the dashboard does not define", () => {
    const out = normalizeValues(DEFS, { ghost: { values: ["x"] } } as FilterValues);
    expect(out.ghost).toBeUndefined();
  });

  it("drops a value outside the option allowlist", () => {
    const out = normalizeValues(DEFS, {
      region: { values: ["EMEA", "'; DROP TABLE orders --"] },
    });
    expect(out.region).toEqual({ values: ["EMEA"] });
  });

  it("collapses an empty selection to all, so it is one cache entry", () => {
    const out = normalizeValues(DEFS, { region: { values: [] } });
    expect(out.region).toEqual({ all: true });
    expect(filterSignature(out)).toBe(
      filterSignature(normalizeValues(DEFS, { region: { all: true } })),
    );
  });

  it("keeps all as a flag rather than expanding it to every option", () => {
    const out = normalizeValues(DEFS, { region: { all: true } });
    expect(out.region).toEqual({ all: true });
  });

  it("de-duplicates repeated values", () => {
    const out = normalizeValues(DEFS, { region: { values: ["US", "US"] } });
    expect(out.region).toEqual({ values: ["US"] });
  });
});

describe("URL round trip", () => {
  // decodeFilters always returns a complete set (an absent key means "the
  // default"), so the fixture is compared against defaults-plus-overrides.
  const complete = (values: FilterValues) =>
    normalizeValues(DEFS, { ...defaultValues(DEFS), ...values });
  const roundTrip = (values: FilterValues) =>
    decodeFilters(DEFS, encodeFilters(DEFS, complete(values)));

  it("survives values containing commas, ampersands and non-ASCII", () => {
    // ClickHouse dimension values routinely contain all three, which is why the
    // encoding uses repeated params rather than a comma-joined list.
    const values = normalizeValues(DEFS, {
      region: { values: ["O'Brien, Ltd & Co", "Ünïcode"] },
    });
    expect(roundTrip(values)).toEqual(complete(values));
  });

  it("survives a custom date range", () => {
    const values = normalizeValues(DEFS, {
      range: { preset: "custom", from: "2026-01-01", to: "2026-03-31" },
    });
    expect(roundTrip(values)).toEqual(complete(values));
  });

  it("survives a preset", () => {
    const values = normalizeValues(DEFS, { range: { preset: "last_90_days" } });
    expect(roundTrip(values)).toEqual(complete(values));
  });

  it("omits defaults, so the canonical view has a clean URL", () => {
    expect(encodeFilters(DEFS, defaultValues(DEFS)).toString()).toBe("");
  });

  it("resolves an absent key back to the default", () => {
    const decoded = decodeFilters(DEFS, new URLSearchParams());
    expect(decoded).toEqual(normalizeValues(DEFS, defaultValues(DEFS)));
    expect(isDefault(DEFS, decoded)).toBe(true);
  });

  it("ignores a param for a filter this dashboard no longer defines", () => {
    const params = new URLSearchParams("f.ghost=x&region=nope");
    expect(decodeFilters(DEFS, params).ghost).toBeUndefined();
  });

  it("treats a null search string as the defaults", () => {
    expect(isDefault(DEFS, decodeFilters(DEFS, null))).toBe(true);
  });
});

describe("isDefault", () => {
  it("is false once a real selection is made", () => {
    expect(isDefault(DEFS, { region: { values: ["EMEA"] } })).toBe(false);
  });
});

describe("sprint filters", () => {
  const SPRINT: FilterDef = {
    id: "sprint",
    kind: "sprint",
    label: "Sprints",
    multi: true,
    options: ["41", "42", "43"],
    option_labels: { "42": "Sprint 42" },
    default: { mode: "last_n", n: 6 },
  };
  const ONE: FilterDef = { ...SPRINT, id: "one", multi: false, default: { mode: "active" } };

  it("round-trips every mode through the URL", () => {
    for (const value of [
      { mode: "active" as const },
      { mode: "all" as const },
      { mode: "last_n" as const, n: 3 },
      { mode: "ids" as const, ids: ["41", "43"] },
    ]) {
      const params = encodeFilters([SPRINT], { sprint: value });
      expect(decodeFilters([SPRINT], params)).toEqual({ sprint: value });
    }
  });

  it("keeps the default out of the URL", () => {
    expect(encodeFilters([SPRINT], { sprint: { mode: "last_n", n: 6 } }).toString()).toBe("");
    expect(isDefault([SPRINT], { sprint: { mode: "last_n", n: 6 } })).toBe(true);
  });

  it("drops what the server would reject, falling back to the default", () => {
    for (const raw of ["bogus", "last:0", "last:99", "last:x", "ids:", "ids:999"]) {
      const params = new URLSearchParams({ sprint: raw });
      expect(decodeFilters([SPRINT], params)).toEqual({ sprint: { mode: "last_n", n: 6 } });
    }
  });

  it("holds a single-sprint filter to one sprint", () => {
    expect(normalizeValues([ONE], { one: { mode: "ids", ids: ["43", "41"] } })).toEqual({
      one: { mode: "ids", ids: ["41"] },
    });
    expect(normalizeValues([ONE], { one: { mode: "last_n", n: 6 } })).toEqual({
      one: { mode: "last_n", n: 1 },
    });
    expect(normalizeValues([ONE], { one: { mode: "all" } })).toEqual({});
  });

  it("gives distinct selections distinct cache keys", () => {
    const a = filterSignature({ sprint: { mode: "ids", ids: ["41"] } });
    const b = filterSignature({ sprint: { mode: "ids", ids: ["42"] } });
    const c = filterSignature({ sprint: { mode: "last_n", n: 41 } });
    expect(new Set([a, b, c]).size).toBe(3);
  });

  it("describes a selection with the sprint's own name", () => {
    expect(describeSprint(SPRINT, { mode: "ids", ids: ["42"] })).toBe("Sprint 42");
    expect(describeSprint(SPRINT, { mode: "last_n", n: 1 })).toBe("Last completed sprint");
    expect(describeSprint(SPRINT, { mode: "ids", ids: ["41", "42"] })).toBe("2 sprints");
  });
});

describe("supportedFilters", () => {
  it("reads filter ids from parameter names, underscores included", () => {
    const params = ["p_issue_type_all", "p_issue_type_values", "p_period_from", "p_sprint_n", "limit"];
    expect(supportedFilters({ params: params.map((name) => ({ name })) })).toEqual(
      new Set(["issue_type", "period", "sprint"]),
    );
    expect(supportedFilters(undefined).size).toBe(0);
  });
});
