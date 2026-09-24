import {
  ApiError,
  apiDelete,
  apiErrorMessage,
  apiGet,
  apiPost,
  apiPut,
} from "./client";
import { streamNdjson } from "./stream";

export interface AuthUser {
  id: string;
  email: string;
  name: string | null;
}

export interface AuthOrg {
  id: string;
  slug: string;
  name: string;
  role: "owner" | "admin" | "member" | "viewer";
}

/**
 * `/api/auth/me` is public on purpose: it answers 200 with
 * `{authenticated: false}` rather than 401, so the app can boot without
 * generating a spurious error on first load.
 */
export type MeResponse =
  | { authenticated: false }
  | {
      authenticated: true;
      user: AuthUser;
      org: AuthOrg | null;
      orgs: AuthOrg[];
      connection: {
        configured: boolean;
        count: number;
        sources: { name: string; type: WarehouseType }[];
      };
    };

export const getMe = (signal?: AbortSignal) =>
  apiGet<MeResponse>("/api/auth/me", signal);

export const login = (email: string, password: string) =>
  apiPost<{ user: AuthUser; org: AuthOrg | null; orgs: AuthOrg[] }>(
    "/api/auth/login",
    { email, password },
  );

export const signup = (email: string, password: string, orgName?: string) =>
  apiPost<{ user: AuthUser; org: AuthOrg }>("/api/auth/signup", {
    email,
    password,
    org_name: orgName || null,
  });

export const logout = () => apiPost<void>("/api/auth/logout");

/**
 * Move the session to another org the user belongs to. This mutates the
 * *server-side* current org on the existing session; the cookie is unchanged.
 * The caller must then hard-reload so no data cached under the old tenant can
 * survive the boundary -- see `switchOrgAndReload`.
 */
export const switchOrg = (orgId: string) =>
  apiPost<{ org: AuthOrg }>(`/api/orgs/${orgId}/switch`);

/**
 * Switch org, then do a full document load rather than a client navigation.
 *
 * A soft `router.push` would keep the React Query cache alive, and carrying a
 * previous tenant's reports/dashboards across the boundary is exactly how a
 * cross-tenant UI leak ships. A hard load drops the entire cache, and landing
 * on `/reports` -- never a deep resource link -- means no stale `/{id}` from
 * the old org is ever requested under the new one.
 */
export async function switchOrgAndReload(orgId: string): Promise<void> {
  await switchOrg(orgId);
  window.location.assign("/reports");
}

// --- per-org data sources ----------------------------------------------------

/**
 * `jira` is not a live engine: it is synced into a read-only Postgres schema
 * on the server and queried there, so it carries a sync state and no scope
 * picker. See `isSyncedSource` in lib/connections/sources.ts.
 */
export type WarehouseType = "clickhouse" | "postgres" | "jira";

export type SslMode =
  | "disable"
  | "allow"
  | "prefer"
  | "require"
  | "verify-ca"
  | "verify-full";

export interface ConnectionInput {
  type: WarehouseType;
  /**
   * The handle the agent types in `run_sql(source: ...)`, so the API constrains
   * it to `^[a-z][a-z0-9_]{0,39}$`.
   */
  name: string;
  /** Free text; goes into the schema catalog and is how the agent routes a
   * question to the right source. */
  description: string;
  is_default: boolean;
  host: string;
  port: number;
  user: string;
  /** Omit or null to keep the stored password -- it is never sent to the UI. */
  password?: string | null;
  database: string;
  secure: boolean;
  /** Postgres only. */
  sslmode?: SslMode | null;
  /**
   * Introspection scope. `introspect_databases` allowlists namespaces
   * (ClickHouse databases, Postgres schemas); `introspect_tables` holds
   * qualified `namespace.table` entries and narrows *within* a namespace -- one
   * named there shows only its listed tables, one absent shows all of them.
   * Both empty means the whole server, which is how sources behaved before the
   * scope picker existed.
   */
  introspect_databases?: string[];
  introspect_tables?: string[];
  /** Jira only: the JQL that scopes what is synced. Empty = everything. */
  scope_query?: string | null;
}

/** How fresh a synced (Jira) source is. Null for live warehouses. */
export interface SyncState {
  status: "never" | "running" | "ok" | "error";
  last_synced_at: string | null;
  error: string | null;
  stats: {
    full?: boolean;
    issues_synced?: number;
    issues_deleted?: number;
    seconds?: number;
    tables?: Record<string, number>;
  };
}

/** Events on the sync stream. `synced` carries the new state; `error` a
 * `code` (`sync_in_progress` | `sync_failed`) and a message. */
export type SyncEvent =
  | { kind: "start"; data: { full: boolean; jql: string; rebuilt: boolean } }
  | { kind: "page"; data: { issues: number } }
  | { kind: "progress"; data: SyncState["stats"] }
  | { kind: "synced"; data: SyncState }
  | { kind: "error"; data: { code: string; message: string } }
  | { kind: "ping"; data: Record<string, never> }
  | { kind: "done"; data: Record<string, never> };

/** One namespace as the discover endpoint reports it. */
export interface DiscoveredNamespace {
  name: string;
  tables: { name: string; rows: number | null; comment: string }[];
}

export interface DiscoverResponse {
  ok: boolean;
  error?: string;
  truncated?: boolean;
  databases?: DiscoveredNamespace[];
}

export interface ConnectionPublic extends Omit<ConnectionInput, "password"> {
  id: string;
  has_password: boolean;
  configured?: boolean;
  sync?: SyncState | null;
  [key: string]: unknown;
}

export const listConnections = (orgId: string) =>
  apiGet<{ connections: ConnectionPublic[] }>(`/api/orgs/${orgId}/connections`);

export const createConnection = (orgId: string, input: ConnectionInput) =>
  apiPost<ConnectionPublic>(`/api/orgs/${orgId}/connections`, input);

export const updateConnection = (
  orgId: string,
  connectionId: string,
  input: ConnectionInput,
) =>
  apiPut<ConnectionPublic>(
    `/api/orgs/${orgId}/connections/${connectionId}`,
    input,
  );

export const deleteConnection = (orgId: string, connectionId: string) =>
  apiDelete<void>(`/api/orgs/${orgId}/connections/${connectionId}`);

export const testConnection = (orgId: string, input: ConnectionInput) =>
  apiPost<{ ok: boolean; error?: string; [key: string]: unknown }>(
    `/api/orgs/${orgId}/connections/test`,
    input,
  );

/**
 * List every namespace and table the candidate connection can see, ignoring
 * its own scope -- the picker has to show what the scope currently excludes.
 * Like `testConnection`, it takes a whole input so it works before a save.
 */
export const discoverConnection = (orgId: string, input: ConnectionInput) =>
  apiPost<DiscoverResponse>(`/api/orgs/${orgId}/connections/discover`, input);

/**
 * Refresh a synced source, streaming progress. `full` re-reads everything,
 * which is the only way deletions in Jira are noticed.
 */
export const syncConnection = (
  orgId: string,
  connectionId: string,
  full = false,
  signal?: AbortSignal,
) =>
  streamNdjson<SyncEvent>(
    `/api/orgs/${orgId}/connections/${connectionId}/sync?full=${full}`,
    {},
    signal,
  );

/** A session that expired mid-use, as opposed to any other failure. */
export function isUnauthorized(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

/**
 * Login/signup failures come back as machine-readable `detail` slugs.
 *
 * The wording lives in `client.ts` alongside every other code the API returns,
 * so a run that dies mid-stream and a failed sign-in say the same thing about
 * the same slug. Deliberately stays vague about whether an account exists.
 */
export const authErrorMessage = apiErrorMessage;

/**
 * The backend answers 409 `no_connection` (not a 502) when an org has no data
 * source configured at all, precisely so the UI can open the settings panel
 * instead of surfacing a driver error. This recognises it wherever it
 * surfaces -- a failed run, a query, an analyze mutation.
 */
export function isNoConnection(err: unknown): boolean {
  return (
    err instanceof ApiError && err.status === 409 && err.message === "no_connection"
  );
}
