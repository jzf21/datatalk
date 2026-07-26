"use client";

import { useCallback, useRef, useState } from "react";

import { describeNetworkError } from "@/lib/api/client";
import { generateContext, type ContextRunEvent } from "@/lib/api/datacontext";

export interface ContextRunState {
  phase: "idle" | "streaming" | "done" | "failed" | "stopped";
  status: string;
  /** The files the run has written so far, newest last. */
  files: { path: string; summary: string; revised: boolean }[];
  entities: string[];
  playbooks: string[];
  /** Recoverable, per-source failures. The run continues past these. */
  warnings: string[];
  fatal: string | null;
  savedCount: number;
}

const IDLE: ContextRunState = {
  phase: "idle",
  status: "",
  files: [],
  entities: [],
  playbooks: [],
  warnings: [],
  fatal: null,
  savedCount: 0,
};

/**
 * Drives one context-generation run.
 *
 * A sibling of `use-ndjson-run` rather than a reuse of it: that hook's reducer
 * models a document-producing run and would silently drop the `file` events
 * that are the whole output here.
 *
 * The fatal rule matches the report runner's: mid-stream `error` events are
 * per-source failures the backend deliberately continues past, so an error is
 * only fatal when the run also produced nothing.
 */
export function useContextRun() {
  const [state, setState] = useState<ContextRunState>(IDLE);
  const abort = useRef<AbortController | null>(null);

  const reset = useCallback(() => {
    abort.current?.abort();
    abort.current = null;
    setState(IDLE);
  }, []);

  const stop = useCallback(() => {
    abort.current?.abort();
    abort.current = null;
    setState((s) => (s.phase === "streaming" ? { ...s, phase: "stopped" } : s));
  }, []);

  const start = useCallback(
    async (
      orgId: string,
      mode: "revise" | "replace",
      confirmOverwrite: boolean,
      onSaved?: () => void,
    ) => {
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      setState({ ...IDLE, phase: "streaming", status: "Starting…" });

      let saved = false;
      try {
        const stream = generateContext(
          orgId,
          { mode, confirm_overwrite: confirmOverwrite },
          controller.signal,
        );
        for await (const event of stream) {
          setState((s) => apply(s, event));
          if (event.kind === "saved") saved = true;
        }
        setState((s) =>
          s.phase === "streaming"
            ? { ...s, phase: s.fatal ? "failed" : "done" }
            : s,
        );
      } catch (err) {
        if (controller.signal.aborted) {
          setState((s) => ({ ...s, phase: "stopped" }));
        } else {
          const message = describeNetworkError(err);
          setState((s) => ({ ...s, phase: "failed", fatal: message }));
        }
      } finally {
        abort.current = null;
        if (saved) onSaved?.();
      }
    },
    [],
  );

  return { state, start, stop, reset, running: state.phase === "streaming" };
}

function apply(s: ContextRunState, e: ContextRunEvent): ContextRunState {
  switch (e.kind) {
    case "status":
      return { ...s, status: e.data.message };
    case "context_plan":
      return { ...s, entities: e.data.entities, playbooks: e.data.playbooks };
    case "file":
      return { ...s, files: [...s.files, e.data] };
    case "error":
      return { ...s, warnings: [...s.warnings, e.data.message] };
    case "saved":
      return { ...s, savedCount: e.data.files, status: "" };
    case "done":
      // A run that emitted no file and never saved failed, whatever else it
      // said. Otherwise the warnings were per-source and the run stands.
      return s.files.length === 0 && s.savedCount === 0
        ? { ...s, fatal: s.warnings.at(-1) ?? "No documentation was produced." }
        : s;
    default:
      return s;
  }
}
