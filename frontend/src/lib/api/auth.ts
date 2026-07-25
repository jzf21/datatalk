import { ApiError, apiGet, apiPost, apiPut } from "./client";

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
      connection: { configured: boolean };
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

// --- per-org ClickHouse connection ------------------------------------------

export interface ConnectionInput {
  host: string;
  port: number;
  user: string;
  /** Omit or null to keep the stored password -- it is never sent to the UI. */
  password?: string | null;
  database: string;
  secure: boolean;
}

export interface ConnectionPublic extends Omit<ConnectionInput, "password"> {
  configured?: boolean;
  [key: string]: unknown;
}

export const getConnection = (orgId: string) =>
  apiGet<ConnectionPublic>(`/api/orgs/${orgId}/connection`);

export const putConnection = (orgId: string, input: ConnectionInput) =>
  apiPut<ConnectionPublic>(`/api/orgs/${orgId}/connection`, input);

export const testConnection = (orgId: string, input: ConnectionInput) =>
  apiPost<{ ok: boolean; error?: string; [key: string]: unknown }>(
    `/api/orgs/${orgId}/connection/test`,
    input,
  );

/** A session that expired mid-use, as opposed to any other failure. */
export function isUnauthorized(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

/**
 * Login/signup failures come back as machine-readable `detail` slugs. They are
 * translated here rather than shown raw, and deliberately stay vague about
 * whether an account exists.
 */
export function authErrorMessage(detail: string): string {
  switch (detail) {
    case "invalid_credentials":
      return "That email and password don't match.";
    case "too_many_attempts":
      return "Too many failed attempts. Try again in 15 minutes.";
    case "email_taken":
      return "An account with that email already exists.";
    case "signup_disabled":
      return "Signup is disabled on this server.";
    case "not_authenticated":
      return "Your session has expired. Please sign in again.";
    case "no_connection":
      return "This workspace isn’t connected to a warehouse yet.";
    default:
      return detail;
  }
}

/**
 * The backend answers 409 `no_connection` (not a 502) when an org has no
 * ClickHouse connection configured, precisely so the UI can open the settings
 * panel instead of surfacing a driver error. This recognises it wherever it
 * surfaces -- a failed run, a query, an analyze mutation.
 */
export function isNoConnection(err: unknown): boolean {
  return (
    err instanceof ApiError && err.status === 409 && err.message === "no_connection"
  );
}
