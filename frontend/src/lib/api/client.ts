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
 * Two servers means two "is it running?" failure modes, and `fetch` reports
 * both as a bare `TypeError: Failed to fetch` with nothing in either log. That
 * is the single most confusing failure in this architecture, so we name it.
 */
export function describeNetworkError(err: unknown): string {
  if (err instanceof ApiError) return err.message;
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
    // A session can expire mid-use. Announce it once, centrally, so the
    // session gate can re-check and drop to the sign-in screen rather than
    // every caller inventing its own 401 branch.
    if (error.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("datatalk:unauthorized"));
    }
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
