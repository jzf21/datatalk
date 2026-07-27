import { describe, expect, it } from "vitest";

import type { ConnectionPublic } from "@/lib/api/auth";
import {
  DATA_SOURCES,
  getDataSource,
  sourceTypeName,
  toConnectionInput,
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

  it("rebuilds a stored source into an input the API accepts", () => {
    const stored = {
      id: "abc",
      type: "clickhouse",
      name: "analytics",
      description: "events",
      is_default: true,
      host: "h.test",
      port: 8123,
      user: "reader",
      database: "default",
      secure: true,
      sslmode: null,
      has_password: true,
      introspect_databases: ["web"],
      introspect_tables: ["web.pageviews"],
    } as ConnectionPublic;

    const input = toConnectionInput(stored);

    // Null, not "": the UI never receives the secret, so this is the only way
    // to save any other field without destroying it.
    expect(input.password).toBeNull();
    expect(input.introspect_databases).toEqual(["web"]);
    expect(input.name).toBe("analytics");
    expect(input.secure).toBe(true);
  });

  it("applies overrides over the stored values", () => {
    // How the scope screen saves one field of a source it never loaded a form
    // for: everything else has to go back unchanged.
    const stored = {
      id: "abc",
      type: "postgres",
      name: "billing",
      description: "",
      is_default: false,
      host: "h.test",
      port: 5432,
      user: "u",
      database: "billing",
      secure: false,
      sslmode: "require",
      has_password: true,
      introspect_databases: ["public"],
      introspect_tables: [],
    } as ConnectionPublic;

    const input = toConnectionInput(stored, {
      introspect_databases: ["reporting"],
      introspect_tables: ["reporting.invoices"],
    });

    expect(input.introspect_databases).toEqual(["reporting"]);
    expect(input.introspect_tables).toEqual(["reporting.invoices"]);
    expect(input.sslmode).toBe("require");
    expect(input.host).toBe("h.test");
  });

  it("gives each engine its own connection defaults", () => {
    // A shared default port is the kind of copy-paste slip that sends a
    // Postgres source at ClickHouse's 8123 and reports a timeout.
    const ports = DATA_SOURCES.map((s) => s.defaults.port);
    expect(new Set(ports).size).toBe(ports.length);
  });
});
