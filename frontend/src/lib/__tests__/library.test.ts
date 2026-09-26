import { describe, expect, it } from "vitest";

import { dayLabel, groupByDay, matchesQuery } from "../library";

const NOW = new Date(2026, 8, 26, 15, 0);

describe("dayLabel", () => {
  it("names today and yesterday by calendar day, not 24h windows", () => {
    expect(dayLabel(new Date(2026, 8, 26, 0, 5).toISOString(), NOW)).toBe("Today");
    expect(dayLabel(new Date(2026, 8, 25, 23, 59).toISOString(), NOW)).toBe(
      "Yesterday",
    );
  });

  it("falls back to Earlier for an unparseable timestamp", () => {
    expect(dayLabel("not a date", NOW)).toBe("Earlier");
  });
});

describe("groupByDay", () => {
  it("keeps input order within and across groups", () => {
    const items = [
      { createdAt: new Date(2026, 8, 26, 14).toISOString(), k: "a" },
      { createdAt: new Date(2026, 8, 26, 9).toISOString(), k: "b" },
      { createdAt: new Date(2026, 8, 25, 9).toISOString(), k: "c" },
    ];
    const groups = groupByDay(items, NOW);
    expect(groups.map(([day]) => day)).toEqual(["Today", "Yesterday"]);
    expect(groups[0][1].map((i) => i.k)).toEqual(["a", "b"]);
  });
});

describe("matchesQuery", () => {
  const run = { id: 12, title: "Which accounts breached SLA most often" };

  it("matches every term in any order, case-insensitively", () => {
    expect(matchesQuery(run, "sla BREACH")).toBe(true);
    expect(matchesQuery(run, "sla revenue")).toBe(false);
  });

  it("matches the id with or without a hash", () => {
    expect(matchesQuery(run, "#12")).toBe(true);
    expect(matchesQuery(run, "12")).toBe(true);
    expect(matchesQuery(run, "#1")).toBe(false);
  });

  it("treats a blank query as match-all", () => {
    expect(matchesQuery(run, "   ")).toBe(true);
  });
});
