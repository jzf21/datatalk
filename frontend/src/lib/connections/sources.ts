import type {
  ConnectionInput,
  ConnectionPublic,
  WarehouseType,
} from "@/lib/api/auth";

/**
 * The catalogue of warehouse engines a workspace can connect to.
 *
 * A workspace holds any number of named sources of any of these types, and the
 * agent picks between them per query. The settings flow asks for the engine
 * *first* and renders its form from `fields`, so adding a third warehouse is a
 * new entry here plus an adapter on the API side -- not a rewrite of the page.
 *
 * `id` is the API's `type` discriminator, not a UI-only label.
 */
export interface DataSourceField {
  id: Exclude<
    keyof ConnectionInput,
    | "secure"
    | "type"
    | "name"
    | "description"
    | "is_default"
    // Drawn by the scope picker, not as a text input.
    | "introspect_databases"
    | "introspect_tables"
  >;
  label: string;
  type?: "text" | "number" | "password" | "select";
  options?: readonly string[];
  /** Shown under the input; the password hint is handled by the page. */
  hint?: string;
}

export interface DataSource {
  id: WarehouseType;
  name: string;
  blurb: string;
  fields: readonly DataSourceField[];
  secureLabel: string;
  /** What `introspect_databases` scopes on this engine. */
  namespaceLabel: string;
  defaults: Omit<ConnectionInput, "type" | "name" | "description" | "is_default">;
}

export const DATA_SOURCES: readonly DataSource[] = [
  {
    id: "clickhouse",
    name: "ClickHouse",
    blurb: "Connect over the HTTP interface — ClickHouse Cloud or self-hosted.",
    fields: [
      { id: "host", label: "Host", hint: "Hostname only, no scheme or port." },
      { id: "port", label: "Port", type: "number" },
      { id: "user", label: "Username" },
      { id: "password", label: "Password", type: "password" },
      { id: "database", label: "Database" },
    ],
    secureLabel: "Use TLS (HTTPS)",
    namespaceLabel: "Databases",
    defaults: {
      host: "",
      port: 8123,
      user: "default",
      password: "",
      database: "default",
      secure: false,
    },
  },
  {
    id: "postgres",
    name: "PostgreSQL",
    blurb:
      "Connect to a Postgres database — every session is opened read-only.",
    fields: [
      { id: "host", label: "Host", hint: "Hostname only, no scheme or port." },
      { id: "port", label: "Port", type: "number" },
      { id: "user", label: "Username" },
      { id: "password", label: "Password", type: "password" },
      {
        id: "database",
        label: "Database",
        hint: "One database per source. Add another source for another database.",
      },
      {
        id: "sslmode",
        label: "SSL mode",
        type: "select",
        options: [
          "prefer",
          "disable",
          "allow",
          "require",
          "verify-ca",
          "verify-full",
        ],
        hint: "Leave on prefer unless your server requires otherwise.",
      },
    ],
    secureLabel: "Require TLS",
    // A Postgres connection sees one database, so the allowlist scopes schemas.
    namespaceLabel: "Schemas",
    defaults: {
      host: "",
      port: 5432,
      user: "postgres",
      password: "",
      database: "postgres",
      secure: false,
      sslmode: null,
    },
  },
];

export function getDataSource(id: string | null): DataSource | null {
  if (id === null) return null;
  return DATA_SOURCES.find((source) => source.id === id) ?? null;
}

/** Engine label for a stored source, for badges and health rows. */
export function sourceTypeName(type: string): string {
  return getDataSource(type)?.name ?? type;
}

/**
 * A stored source back into the shape create/update/discover accept.
 *
 * The scope screen edits one field of a source it did not load a form for, and
 * the API takes whole connections rather than patches -- so it has to send the
 * rest back unchanged. `password: null` is what keeps the stored secret: the
 * UI never receives it and so can never echo it.
 */
export function toConnectionInput(
  connection: ConnectionPublic,
  overrides: Partial<ConnectionInput> = {},
): ConnectionInput {
  return {
    type: connection.type,
    name: connection.name,
    description: connection.description ?? "",
    is_default: Boolean(connection.is_default),
    host: connection.host,
    port: connection.port,
    user: connection.user,
    password: null,
    database: connection.database,
    secure: Boolean(connection.secure),
    sslmode: connection.sslmode ?? null,
    introspect_databases: connection.introspect_databases ?? [],
    introspect_tables: connection.introspect_tables ?? [],
    ...overrides,
  };
}
