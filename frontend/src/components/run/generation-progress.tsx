"use client";

import { useState } from "react";
import { CircleAlert, Sparkles } from "lucide-react";

import type { RunState } from "@/hooks/use-ndjson-run";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { QueryCard } from "./query-card";

/** `max_steps` in sqlloop.py -- an honest counter beats a fake percentage. */
const MAX_STEPS = 8;

/**
 * The run sheet. The old UI showed a <pre> tail; this turns the wait into
 * something readable, and -- crucially -- something the user can judge and
 * abandon early.
 */
export function GenerationProgress({
  state,
  onStop,
  onRefine,
}: {
  state: RunState;
  onStop: () => void;
  onRefine: () => void;
}) {
  const streaming = state.phase === "streaming";

  return (
    <div className="space-y-6">
      {/* Phase transitions only. Announcing every SQL string would be unusable. */}
      <p role="status" aria-live="polite" className="sr-only">
        {state.fatal
          ? "Generation failed."
          : state.document
            ? "Report ready."
            : state.plan.length > 0
              ? `Plan ready, ${state.plan.length} sections.`
              : streaming
                ? "Generating."
                : ""}
      </p>

      {streaming && (
        <div className="flex items-center gap-3">
          <span className="h-0.5 flex-1 overflow-hidden rounded-full bg-muted">
            <span className="shimmer block h-full w-full" />
          </span>
          <Button variant="outline" size="sm" onClick={onStop}>
            Stop watching
          </Button>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[380px_minmax(0,1fr)]">
        <div className="space-y-4">
          {state.memory.length > 0 && <MemoryChip rules={state.memory} />}
          {state.plan.length > 0 && (
            <PlanPanel
              sections={state.plan}
              stoppable={streaming}
              onRefine={onRefine}
            />
          )}
        </div>

        <div>
          <div className="mb-2 flex items-baseline justify-between">
            <h2 className="label-caps text-ink-tertiary">Run log</h2>
            {streaming && (
              <span className="cite text-ink-tertiary">
                step {Math.min(state.steps.length, MAX_STEPS)} / {MAX_STEPS}
              </span>
            )}
          </div>

          {state.status && (
            <p className="mb-2 text-[13px] text-ink-secondary">{state.status}</p>
          )}

          {state.steps.length > 0 ? (
            <RunLog state={state} />
          ) : (
            <div className="space-y-2" aria-hidden>
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-8 w-full" />
              ))}
            </div>
          )}
        </div>
      </div>

      {state.fatal && <FatalError message={state.fatal} state={state} />}

      {!state.document && !state.fatal && state.plan.length > 0 && (
        <DocumentSkeleton sections={state.plan.length} />
      )}
    </div>
  );
}

function RunLog({ state }: { state: RunState }) {
  const [expanded, setExpanded] = useState(false);
  const RECENT = 4;
  const hidden = Math.max(0, state.steps.length - RECENT);
  const shown = expanded ? state.steps : state.steps.slice(-RECENT);

  return (
    <div
      role="log"
      aria-live="off"
      className="rounded-[6px] border border-border bg-card"
    >
      {hidden > 0 && !expanded && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="w-full border-b border-border px-3 py-1.5 text-left text-[12px] text-ink-secondary hover:bg-accent/50"
        >
          ▸ {hidden} earlier {hidden === 1 ? "step" : "steps"}
        </button>
      )}
      {shown.map((step, i) => (
        <QueryCard key={i} step={step} />
      ))}
    </div>
  );
}

function MemoryChip({ rules }: { rules: string[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-[6px] border border-border bg-card p-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-[13px] text-ink-secondary hover:text-ink-primary"
      >
        <Sparkles className="size-3.5" aria-hidden />
        {rules.length} house {rules.length === 1 ? "rule" : "rules"} applied
        <span className="text-ink-tertiary">{open ? "▴" : "▾"}</span>
      </button>
      {/* The payload already carries the texts; the old UI printed only a
          count, so this was the one moment users could never see. */}
      {open && (
        <ul className="mt-2 space-y-1 border-t border-border pt-2">
          {rules.map((rule, i) => (
            <li key={i} className="text-[12px] leading-snug text-ink-secondary">
              {rule}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function PlanPanel({
  sections,
  stoppable,
  onRefine,
}: {
  sections: RunState["plan"];
  stoppable: boolean;
  onRefine: () => void;
}) {
  return (
    <div className="rounded-[6px] border border-border bg-card p-4">
      <h2 className="label-caps mb-3 text-ink-tertiary">Plan</h2>
      <ol className="space-y-3">
        {sections.map((section, i) => (
          <li key={section.id ?? i} className="flex gap-2.5">
            <span className="cite mt-0.5 text-ink-disabled">{i + 1}</span>
            <div className="min-w-0">
              <p className="text-[15px] font-semibold text-ink-primary">
                {section.title}
              </p>
              {section.goal && (
                <p className="mt-0.5 text-[13px] text-ink-secondary">
                  {section.goal}
                </p>
              )}
              {section.data_questions?.length > 0 && (
                <ul className="mt-1 space-y-0.5">
                  {section.data_questions.map((q, qi) => (
                    <li key={qi} className="text-[12px] text-ink-tertiary">
                      · {q}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </li>
        ))}
      </ol>

      {/* Killing a bad run 8 seconds in, instead of at 90, is the highest-value
          interaction in the product. */}
      {stoppable && (
        <button
          type="button"
          onClick={onRefine}
          className="mt-4 text-[12px] text-ink-secondary underline underline-offset-2 hover:text-ink-primary"
        >
          Plan looks wrong? Stop &amp; refine
        </button>
      )}
    </div>
  );
}

function FatalError({ message, state }: { message: string; state: RunState }) {
  const lastSql = state.steps.at(-1)?.sql;
  return (
    <div
      role="alert"
      className="rounded-[6px] border border-border border-l-2 border-l-destructive bg-card p-4"
    >
      <h2 className="flex items-center gap-2 text-[15px] font-semibold text-ink-primary">
        <CircleAlert className="size-4 text-destructive" aria-hidden />
        Generation failed
      </h2>
      <p className="cite mt-2 text-ink-secondary">{message}</p>
      {lastSql && (
        <pre className="cite mt-3 max-h-40 overflow-auto rounded-[4px] border border-border bg-muted p-2 text-ink-primary">
          {lastSql}
        </pre>
      )}
      <p className="mt-3 text-[12px] text-ink-tertiary">
        The plan and run log above are still yours — the queries that succeeded
        can be copied from them.
      </p>
    </div>
  );
}

/** Shaped from the plan, so the document reads as being drafted, not awaited. */
function DocumentSkeleton({ sections }: { sections: number }) {
  return (
    <div className="space-y-8" aria-hidden>
      {Array.from({ length: sections }, (_, i) => (
        <div key={i} className="space-y-3">
          <Skeleton className="h-5 w-[60%]" />
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-[96%]" />
          <Skeleton className="h-3 w-[72%]" />
          <Skeleton className="h-[220px] w-full" />
        </div>
      ))}
    </div>
  );
}
