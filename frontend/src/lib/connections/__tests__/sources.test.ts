import { describe, expect, it } from "vitest";

import {
  DATA_SOURCES,
  getDataSource,
  sourceTypeName,
} from "@/lib/connections/sources";

describe("data sources", () => {
  it("resolves each engine the API can store", () => {
    expect(getDataSource("clickhouse")?.name).toBe("ClickHouse");
    expect(getDataSource("postgres")?.name).toBe("PostgreSQL");
  });

  it("returns null for an unknown or unpicked source", () => {
    expect(getDataSource(null)).toBeNull();
    expect(getDataSource("duckdb")).toBeNull();
  });

  it("falls back to the raw type when the UI does not know an engine", () => {
    // A source stored by a newer backend must still render a readable badge.
    expect(sourceTypeName("duckdb")).toBe("duckdb");
    expect(sourceTypeName("postgres")).toBe("PostgreSQL");
  });

  it("has ids the settings form can round-trip", () => {
    for (const source of DATA_SOURCES) {
      expect(getDataSource(source.id)).toBe(source);
      // Every rendered field must have a seed value, or the input goes
      // uncontrolled the moment the user types into it.
      for (const field of source.fields) {
        expect(source.defaults).toHaveProperty(field.id);
      }
    }
  });

  it("gives each engine its own connection defaults", () => {
    // A shared default port is the kind of copy-paste slip that sends a
    // Postgres source at ClickHouse's 8123 and reports a timeout.
    const ports = DATA_SOURCES.map((s) => s.defaults.port);
    expect(new Set(ports).size).toBe(ports.length);
  });
});
