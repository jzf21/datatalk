import { describe, expect, it } from "vitest";

import {
  ALL,
  isTableSelected,
  isUnscoped,
  namespaceState,
  parseScope,
  scopeSummary,
  toScopeArrays,
  toggleTable,
  withNamespace,
  type Scope,
} from "@/lib/connections/scope";

/** `parse -> toArrays` must be the identity for anything the API can store. */
function roundTrip(databases: string[], tables: string[]) {
  return toScopeArrays(parseScope(databases, tables));
}

describe("parseScope", () => {
  it("treats an empty scope as the whole server", () => {
    // Every source predating the picker has empty arrays, and must keep
    // behaving exactly as it did.
    expect(isUnscoped(parseScope([], []))).toBe(true);
    expect(isUnscoped(parseScope(undefined, undefined))).toBe(true);
  });

  it("reads a namespace with no table entries as all of its tables", () => {
    const scope = parseScope(["billing"], []);
    expect(scope.get("billing")).toBe(ALL);
    expect(namespaceState(scope, "billing")).toBe("checked");
  });

  it("reads a namespace with table entries as only those tables", () => {
    const scope = parseScope(
      ["analytics"],
      ["analytics.events", "analytics.sessions"],
    );
    expect(scope.get("analytics")).toEqual(new Set(["events", "sessions"]));
    expect(namespaceState(scope, "analytics")).toBe("partial");
  });

  it("scopes table entries to their own namespace only", () => {
    // The rule the whole design rests on: naming tables in one database must
    // not narrow a different one.
    const scope = parseScope(
      ["analytics", "billing"],
      ["analytics.events"],
    );
    expect(scope.get("analytics")).toEqual(new Set(["events"]));
    expect(scope.get("billing")).toBe(ALL);
  });

  it("drops entries for a namespace that is not in scope", () => {
    // The API applies the namespace allowlist first, so such an entry selects
    // nothing; showing it ticked would misreport what the agent sees.
    const scope = parseScope(["analytics"], ["staging.events"]);
    expect(scope.has("staging")).toBe(false);
    expect(scope.get("analytics")).toBe(ALL);
  });

  it("ignores malformed entries rather than guessing", () => {
    const scope = parseScope(["analytics"], ["events", "analytics.", ".events"]);
    expect(scope.get("analytics")).toBe(ALL);
  });

  it("reports a namespace outside the scope as unchecked", () => {
    expect(namespaceState(parseScope(["a"], []), "b")).toBe("unchecked");
    expect(isTableSelected(parseScope(["a"], []), "b", "t")).toBe(false);
  });
});

describe("toScopeArrays", () => {
  it("round-trips a whole-namespace selection", () => {
    expect(roundTrip(["billing"], [])).toEqual({
      introspect_databases: ["billing"],
      introspect_tables: [],
    });
  });

  it("round-trips a mixed selection", () => {
    const arrays = roundTrip(
      ["analytics", "billing"],
      ["analytics.events", "analytics.sessions"],
    );
    expect(arrays).toEqual({
      introspect_databases: ["analytics", "billing"],
      introspect_tables: ["analytics.events", "analytics.sessions"],
    });
  });

  it("drops a namespace selected down to zero tables", () => {
    // An empty selection would parse back as ALL, silently re-including
    // everything the user just cleared.
    const scope: Scope = new Map([["analytics", new Set<string>()]]);
    expect(toScopeArrays(scope)).toEqual({
      introspect_databases: [],
      introspect_tables: [],
    });
  });
});

describe("toggleTable", () => {
  const tables = ["events", "sessions", "users"];

  it("expands ALL into the survivors when one table is unticked", () => {
    const scope = toggleTable(parseScope(["a"], []), "a", "users", tables);
    expect(scope.get("a")).toEqual(new Set(["events", "sessions"]));
  });

  it("collapses back to ALL once every table is ticked again", () => {
    // Not merely cosmetic: ALL is what keeps the source picking up tables
    // created after this was saved.
    let scope = toggleTable(parseScope(["a"], []), "a", "users", tables);
    scope = toggleTable(scope, "a", "users", tables);
    expect(scope.get("a")).toBe(ALL);
  });

  it("removes the namespace when its last table is unticked", () => {
    let scope = parseScope(["a"], ["a.events"]);
    scope = toggleTable(scope, "a", "events", tables);
    expect(scope.has("a")).toBe(false);
  });

  it("adds a table to a namespace that was not in scope", () => {
    const scope = toggleTable(new Map(), "a", "events", tables);
    expect(scope.get("a")).toEqual(new Set(["events"]));
  });
});

describe("withNamespace", () => {
  it("removes a namespace set to null or to nothing", () => {
    const scope = parseScope(["a", "b"], []);
    expect(withNamespace(scope, "a", null).has("a")).toBe(false);
    expect(withNamespace(scope, "a", new Set()).has("a")).toBe(false);
  });

  it("does not mutate the scope it was given", () => {
    const scope = parseScope(["a"], []);
    withNamespace(scope, "b", ALL);
    expect(scope.has("b")).toBe(false);
  });
});

describe("scopeSummary", () => {
  it("says so when nothing is narrowed", () => {
    expect(scopeSummary(parseScope([], []))).toBe("All databases");
  });

  it("counts namespaces, and against the total when it is known", () => {
    const scope = parseScope(["a", "b"], []);
    expect(scopeSummary(scope)).toBe("2 databases");
    expect(scopeSummary(scope, 7)).toBe("2 of 7 databases");
  });

  it("mentions the namespaces narrowed to specific tables", () => {
    const scope = parseScope(["a", "b"], ["a.events"]);
    expect(scopeSummary(scope)).toBe(
      "2 databases, 1 narrowed to selected tables",
    );
  });
});
