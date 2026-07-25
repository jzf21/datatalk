"use client";

import { useCallback, useReducer, useRef } from "react";

import { describeNetworkError } from "@/lib/api/client";
import type {
  BlockDocument,
  CapturedQuery,
  PlanSection,
  RunEvent,
} from "@/lib/api/types";

export interface QueryStep {
  /** Assigned on the `result` event; undefined while the query is in flight. */
  datasetId?: string;
  sql: string;
  rowCount?: number;
  columns?: string[];
  /** Recoverable DB errors the model was fed back and retried after. */
  retries: string[];
  state: "running" | "done";
}

export interface RunState {
  phase: "idle" | "streaming" | "done" | "failed" | "stopped";
  request: string;
  status: string | null;
  memory: string[];
  plan: PlanSection[];
  steps: QueryStep[];
  document: BlockDocument | null;
  queries: CapturedQuery[];
  savedId: number | null;
  turnId: number | null;
  fatal: string | null;
}

const EMPTY: RunState = {
  phase: "idle",
  request: "",
  status: null,
  memory: [],
  plan: [],
  steps: [],
  document: null,
  queries: [],
  savedId: null,
  turnId: null,
  fatal: null,
};

type Action =
  | { type: "start"; request: string }
  | { type: "event"; event: RunEvent }
  | { type: "fail"; message: string }
  | { type: "stop" }
  | { type: "reset" };

function reducer(state: RunState, action: Action): RunState {
  switch (action.type) {
    case "start":
      return { ...EMPTY, phase: "streaming", request: action.request };

    case "fail":
      return { ...state, phase: "failed", fatal: action.message };

    case "stop":
      return { ...state, phase: "stopped" };

    case "reset":
      return EMPTY;

    case "event":
      return applyEvent(state, action.event);
  }
}

function applyEvent(state: RunState, event: RunEvent): RunState {
  switch (event.kind) {
    case "memory":
      return { ...state, memory: event.data.suggestions ?? [] };

    case "status":
      return { ...state, status: event.data.message };

    case "plan":
      return { ...state, plan: event.data.sections ?? [] };

    case "sql":
      return {
        ...state,
        steps: [
          ...state.steps,
          { sql: event.data.sql, retries: [], state: "running" },
        ],
      };

    case "result": {
      // Resolve the most recent in-flight step in place.
      const steps = [...state.steps];
      for (let i = steps.length - 1; i >= 0; i--) {
        if (steps[i].state === "running") {
          steps[i] = {
            ...steps[i],
            datasetId: event.data.dataset_id,
            rowCount: event.data.row_count,
            columns: event.data.columns,
            state: "done",
          };
          break;
        }
      }
      return { ...state, steps };
    }

    case "error": {
      // Every mid-stream error is fed back to the model as tool content, so it
      // is recoverable *by construction*. Only an error arriving after the
      // worker has stopped -- with no document produced -- is fatal, and that
      // one is decided by the caller when the stream ends.
      const steps = [...state.steps];
      const last = steps.length - 1;
      if (last >= 0) {
        steps[last] = {
          ...steps[last],
          retries: [...steps[last].retries, event.data.message],
        };
        return { ...state, steps };
      }
      return { ...state, fatal: event.data.message };
    }

    case "report":
    case "dashboard":
      return { ...state, document: event.data.document, status: null };

    case "answer":
      return {
        ...state,
        document: event.data.document,
        queries: event.data.queries ?? [],
        turnId: event.data.turn_id,
        status: null,
      };

    case "saved":
      return {
        ...state,
        queries: event.data.queries ?? state.queries,
        savedId: event.data.report_id ?? event.data.dashboard_id ?? null,
      };

    case "done":
      return { ...state, phase: "done", status: null };
  }
}

/**
 * Drive one NDJSON run.
 *
 * The stream lives in a reducer rather than in TanStack Query: it is
 * high-frequency, ephemeral, never shared and never worth caching.
 */
export function useNdjsonRun() {
  const [state, dispatch] = useReducer(reducer, EMPTY);
  const abortRef = useRef<AbortController | null>(null);

  const start = useCallback(
    async (
      request: string,
      run: (signal: AbortSignal) => AsyncGenerator<RunEvent>,
      onSaved?: (id: number | null) => void,
    ) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      dispatch({ type: "start", request });

      let sawDocument = false;
      let lastError: string | null = null;
      let savedId: number | null = null;

      try {
        for await (const event of run(controller.signal)) {
          if (event.kind === "report" || event.kind === "dashboard" || event.kind === "answer") {
            sawDocument = true;
          }
          if (event.kind === "error") lastError = event.data.message;
          if (event.kind === "saved") {
            savedId = event.data.report_id ?? event.data.dashboard_id ?? null;
          }
          dispatch({ type: "event", event });
        }

        // The fatal rule, in one place: an error is fatal iff no document ever
        // arrived. The clean fix is a `fatal` flag on the backend event.
        if (!sawDocument && lastError) {
          dispatch({ type: "fail", message: lastError });
        } else {
          // The server always sends `done`, but a stream cut short would
          // otherwise leave the UI stuck in "streaming" forever.
          dispatch({ type: "event", event: { kind: "done", data: {} } });
          onSaved?.(savedId);
        }
      } catch (err) {
        if (controller.signal.aborted) dispatch({ type: "stop" });
        else dispatch({ type: "fail", message: describeNetworkError(err) });
      } finally {
        abortRef.current = null;
      }
    },
    [],
  );

  /**
   * Stops *watching*. The server runs generation on a daemon thread and
   * persists the result regardless, so the run will still appear in the list.
   */
  const stop = useCallback(() => abortRef.current?.abort(), []);
  const reset = useCallback(() => dispatch({ type: "reset" }), []);

  return { state, start, stop, reset, running: state.phase === "streaming" };
}
