"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";

import { describeNetworkError } from "@/lib/api/client";
import type {
  BlockDocument,
  CapturedQuery,
  InsightSet,
  PlanSection,
  RunEvent,
} from "@/lib/api/types";

export interface QueryStep {
  /** Correlates this step with its result/error events within a turn. */
  queryId?: string;
  /** Assigned on the `result` event; undefined while the query is in flight. */
  datasetId?: string;
  sql: string;
  source?: string | null;
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
  /** Analyst-loop turn counter, straight from the backend's status events. */
  step: number | null;
  maxSteps: number | null;
  memory: string[];
  plan: PlanSection[];
  steps: QueryStep[];
  /** Structured findings from the insight pass (dashboard runs only). */
  insights: InsightSet | null;
  document: BlockDocument | null;
  queries: CapturedQuery[];
  savedId: number | null;
  turnId: number | null;
  fatal: string | null;
}

export const EMPTY: RunState = {
  phase: "idle",
  request: "",
  status: null,
  step: null,
  maxSteps: null,
  memory: [],
  plan: [],
  steps: [],
  insights: null,
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

/** Index of the step a result/error event belongs to: match the in-flight step
 * by query_id when the event carries one (batched queries finish in any
 * order), else the most recent in-flight step (older backends). */
function stepIndexFor(steps: QueryStep[], queryId: string | undefined): number {
  let lastRunning = -1;
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].state !== "running") continue;
    if (queryId && steps[i].queryId === queryId) return i;
    if (lastRunning < 0) lastRunning = i;
  }
  return lastRunning;
}

export function applyEvent(state: RunState, event: RunEvent): RunState {
  switch (event.kind) {
    case "memory":
      return { ...state, memory: event.data.suggestions ?? [] };

    case "status":
      return {
        ...state,
        status: event.data.message,
        step: event.data.step ?? state.step,
        maxSteps: event.data.max_steps ?? state.maxSteps,
      };

    case "plan":
      return { ...state, plan: event.data.sections ?? [] };

    case "sql":
      return {
        ...state,
        steps: [
          ...state.steps,
          {
            queryId: event.data.query_id,
            sql: event.data.sql,
            source: event.data.source,
            retries: [],
            state: "running",
          },
        ],
      };

    case "result": {
      // Batched queries run concurrently on the server, so results can land in
      // any order: resolve by query_id when the event carries one, falling back
      // to the most recent in-flight step for older backends.
      const steps = [...state.steps];
      const target = stepIndexFor(steps, event.data.query_id);
      if (target >= 0) {
        steps[target] = {
          ...steps[target],
          datasetId: event.data.dataset_id,
          source: event.data.source ?? steps[target].source,
          rowCount: event.data.row_count,
          columns: event.data.columns,
          state: "done",
        };
      }
      return { ...state, steps };
    }

    case "error": {
      // Every mid-stream error is fed back to the model as tool content, so it
      // is recoverable *by construction*. Only an error arriving after the
      // worker has stopped -- with no document produced -- is fatal, and that
      // one is decided by the caller when the stream ends.
      const steps = [...state.steps];
      const byId = stepIndexFor(steps, event.data.query_id);
      const target = byId >= 0 && event.data.query_id ? byId : steps.length - 1;
      if (target >= 0) {
        steps[target] = {
          ...steps[target],
          retries: [...steps[target].retries, event.data.message],
        };
        return { ...state, steps };
      }
      // No step to hang it on -- an error before any SQL ran. Declaring it
      // fatal here would contradict the rule above: a run that captured nothing
      // still produces a document explaining why, and that is not a failure.
      // The stream-end caller tracks the last error and decides.
      return { ...state, status: event.data.message };
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

    case "insights":
      // The findings arrive before the author call, so the panel gives the
      // user something real to read during the longest silent stretch.
      return { ...state, insights: event.data };

    case "done":
      return { ...state, phase: "done", status: null };

    default:
      // A backend newer than this bundle may stream kinds we cannot render.
      // Ignoring one costs a progress nicety; crashing the reducer costs the run.
      return state;
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
      /** `signal` aborts if the run is stopped, reset or unmounted -- check it
       *  before acting on the result after any further await. */
      onSaved?: (id: number | null, signal: AbortSignal) => void,
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
          onSaved?.(savedId, controller.signal);
        }
      } catch (err) {
        // A reset, or a newer run, has already replaced this one's state; the
        // abort it caused must not land on top as a late "stopped".
        if (abortRef.current !== controller) return;
        if (controller.signal.aborted) dispatch({ type: "stop" });
        else dispatch({ type: "fail", message: describeNetworkError(err) });
      }
      // abortRef keeps the finished controller: aborting it after the stream
      // ends is harmless, and is how stop/reset/unmount cancel whatever the
      // onSaved callback is still awaiting.
    },
    [],
  );

  // Leaving the page stops watching (the server persists the run regardless),
  // so a stream from a page you left can never act on the page you are on.
  useEffect(() => () => abortRef.current?.abort(), []);

  /**
   * Stops *watching*. The server runs generation on a daemon thread and
   * persists the result regardless, so the run will still appear in the list.
   */
  const stop = useCallback(() => abortRef.current?.abort(), []);
  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    dispatch({ type: "reset" });
  }, []);

  return { state, start, stop, reset, running: state.phase === "streaming" };
}
