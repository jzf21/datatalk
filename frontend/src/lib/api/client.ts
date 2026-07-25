/** Base HTTP client for the DataTalk API (a different origin -- see CORS). */

/**
 * Default to `localhost`, NOT `127.0.0.1`.
 *
 * The session cookie is set with `SameSite=Lax`, which withholds it from
 * cross-*site* XHR. `localhost:3000 -> localhost:8000` is same-site (ports are
 * not part of a site), so the cookie travels; `localhost:3000 -> 127.0.0.1:8000`
 * is cross-site, so it does not, and every authenticated request 401s with no
 * visible cause. Keep the hostnames matching.
 */
export const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * The API answers with stable machine codes in `detail` (see web/deps.py), so
 * the wording lives here in one place rather than in each caller. Anything
 * unrecognised falls through unchanged -- better a raw slug than a wrong guess.
 */
const DETAIL_MESSAGES: Record<string, string> = {
  not_authenticated: "Your session has expired. Please sign in again.",
  no_org: "You're not a member of any workspace yet. Ask an admin to invite you.",
  forbidden: "You don't have permission to do that.",
  no_connection: "This workspace isn't connected to a warehouse yet.",
  cross_origin_request: "That request was blocked as cross-origin.",
  invalid_credentials: "That email and password don't match.",
  too_many_attempts: "Too many failed attempts. Try again in 15 minutes.",
  email_taken: "An account with that email already exists.",
  signup_disabled: "Signup is disabled on this server.",
};

export function apiErrorMessage(detail: string): string {
  return DETAIL_MESSAGES[detail] ?? detail;
}

/**
 * Broadcast the two failures the whole app has to react to, not just the caller.
 *
 * A 401 means the session died and the shell should drop to sign-in; a 409
 * `no_connection` means this org has no warehouse and the only useful next
 * screen is the connection panel. Announced as events so both the plain and
 * streaming fetch paths share one contract.
 */
export function announceApiError(error: ApiError): void {
  if (typeof window === "undefined") return;
  if (error.status === 401) {
    window.dispatchEvent(new CustomEvent("datatalk:unauthorized"));
  } else if (error.status === 409 && error.message === "no_connection") {
    window.dispatchEvent(new CustomEvent("datatalk:no-connection"));
  }
}

/**
 * Two servers means two "is it running?" failure modes, and `fetch` reports
 * both as a bare `TypeError: Failed to fetch` with nothing in either log. That
 * is the single most confusing failure in this architecture, so we name it.
 */
export function describeNetworkError(err: unknown): string {
  if (err instanceof ApiError) return apiErrorMessage(err.message);
  if (err instanceof DOMException && err.name === "AbortError") return "Stopped.";
  if (err instanceof TypeError) {
    return `Could not reach the DataTalk API at ${API_BASE}. Is uvicorn running, and is this origin allowed by DATATALK_CORS_ORIGINS?`;
  }
  return err instanceof Error ? err.message : String(err);
}

/** Pull FastAPI's `{"detail": ...}` off a non-2xx response. */
async function toApiError(res: Response): Promise<ApiError> {
  let detail = res.statusText || `HTTP ${res.status}`;
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") detail = body.detail;
    else if (body?.detail) detail = JSON.stringify(body.detail);
  } catch {
    // Not JSON -- keep the status text.
  }
  return new ApiError(res.status, detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    // Auth is a session cookie on another origin: without this it is never
    // sent, and every call 401s.
    credentials: "include",
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    const error = await toApiError(res);
    announceApiError(error);
    throw error;
  }
  return (await res.json()) as T;
}

export function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: "GET", signal });
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}

export function apiPut<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) });
}

export function apiDelete<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}
