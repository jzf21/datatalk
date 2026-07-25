import { API_BASE, ApiError, announceApiError } from "./client";
import type { RunEvent } from "./types";

/**
 * Read an NDJSON response body as a typed event stream.
 *
 * Three things here are load-bearing and easy to get wrong:
 *
 * - `decode(value, { stream: true })` is NOT optional. Without it a multi-byte
 *   character split across two chunks is silently corrupted, and ClickHouse
 *   data is full of non-ASCII.
 * - The tail must be flushed after the reader is done: the server's final line
 *   arrives with a trailing newline today, but a stream that ends without one
 *   would otherwise drop its last event.
 * - A single line can be multi-MB (the `report` event carries the whole
 *   document, and a table may hold SQL_MAX_ROWS rows), so lines are buffered
 *   until a newline rather than assumed to fit in one chunk.
 */
export async function* readNdjson(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<RunEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let newline: number;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        const event = parseLine(line);
        if (event) yield event;
      }
    }

    buffer += decoder.decode(); // flush any partial multi-byte sequence
    const tail = parseLine(buffer);
    if (tail) yield tail;
  } finally {
    reader.releaseLock();
  }
}

function parseLine(line: string): RunEvent | null {
  const trimmed = line.trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed) as RunEvent;
  } catch {
    // A malformed line should not kill a run that is otherwise fine.
    return null;
  }
}

/**
 * POST a JSON body and stream the NDJSON response.
 *
 * Validation failures (empty request, unknown report) come back as an ordinary
 * HTTP 4xx with a `{"detail": ...}` body *before* the stream starts, so they
 * are thrown as ApiError rather than surfacing as an in-stream `error` event.
 */
export async function* streamNdjson(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<RunEvent> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    credentials: "include", // the session cookie; see client.ts
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;
    try {
      const parsed = await res.json();
      if (typeof parsed?.detail === "string") detail = parsed.detail;
    } catch {
      /* not JSON */
    }
    const error = new ApiError(res.status, detail);
    // Same contract as the plain client: a run that dies on an expired session
    // or a missing connection must move the whole app, not just this one run.
    announceApiError(error);
    throw error;
  }
  if (!res.body) throw new ApiError(res.status, "Response carried no body.");

  yield* readNdjson(res.body);
}
