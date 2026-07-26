"use client";

import Link from "next/link";

import { ApiError } from "@/lib/api/client";
import { useHealth } from "@/lib/api/queries";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { cn } from "@/lib/utils";

function Dot({ state }: { state: "ok" | "bad" | "unknown" }) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block size-[7px] rounded-full",
        state === "ok" && "bg-status-good",
        state === "bad" && "bg-status-critical",
        state === "unknown" && "bg-ink-disabled",
      )}
    />
  );
}

export function HealthPill() {
  const { data, isPending, isError, error } = useHealth();

  // /api/health needs a warehouse, so it 409s for an unconfigured workspace.
  // That is a setup step, not an outage -- calling it "API unreachable" would
  // send someone to check uvicorn for no reason.
  const unconfigured = error instanceof ApiError && error.status === 409;

  const sources = isPending || isError ? [] : data.sources;
  const healthy = sources.filter((s) => s.ok).length;
  // One dot for the sources as a whole: all up, some up, or none. A workspace
  // can have five warehouses and the pill still has to fit in the header.
  const wh: "ok" | "bad" | "unknown" =
    isPending || isError
      ? "unknown"
      : sources.length > 0 && healthy === sources.length
        ? "ok"
        : "bad";
  const oa = isPending || isError ? "unknown" : data.openai.ok ? "ok" : "bad";

  // Status is never colour alone -- the dots are decorative and the label
  // carries the meaning.
  const label = unconfigured
    ? "Not connected"
    : isError
      ? "API unreachable"
      : isPending
        ? "Checking…"
        : wh === "ok" && oa === "ok"
          ? sources.length > 1
            ? `${sources.length} sources`
            : "Connected"
          : "Degraded";

  return (
    <HoverCard openDelay={150}>
      <HoverCardTrigger asChild>
        <Link
          href="/settings/connection"
          className="flex items-center gap-2 rounded-[4px] px-1 py-1 text-[12px] text-ink-secondary hover:text-ink-primary"
        >
          <span className="flex gap-1">
            <Dot state={wh} />
            <Dot state={oa} />
          </span>
          {label}
        </Link>
      </HoverCardTrigger>

      <HoverCardContent side="top" align="start" className="w-72 text-[13px]">
        <dl className="space-y-2">
          {unconfigured || isError || isPending ? (
            <div>
              <dt className="label-caps text-ink-secondary">Data sources</dt>
              <dd className="cite text-ink-primary">
                {unconfigured
                  ? "None configured"
                  : isError
                    ? "API unreachable"
                    : "…"}
              </dd>
            </div>
          ) : (
            // One row per source: with several warehouses, "degraded" is
            // useless unless it says which one is down.
            data.sources.map((s) => (
              <div key={s.name}>
                <dt className="label-caps flex items-center gap-1.5 text-ink-secondary">
                  <Dot state={s.ok ? "ok" : "bad"} />
                  {s.name}
                </dt>
                <dd className="cite text-ink-primary">
                  {s.ok
                    ? `${s.database} · ${s.table_count} tables · v${s.version}`
                    : s.error}
                </dd>
              </div>
            ))
          )}
          <div>
            <dt className="label-caps text-ink-secondary">OpenAI</dt>
            <dd className="cite text-ink-primary">
              {unconfigured
                ? "No connection configured"
                : isError
                ? "API unreachable"
                : isPending
                  ? "…"
                  : data.openai.ok
                    ? data.openai.model
                    : data.openai.error}
            </dd>
          </div>
        </dl>
      </HoverCardContent>
    </HoverCard>
  );
}
