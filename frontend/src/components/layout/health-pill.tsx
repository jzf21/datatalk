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

  const ch = isPending || isError ? "unknown" : data.clickhouse.ok ? "ok" : "bad";
  const oa = isPending || isError ? "unknown" : data.openai.ok ? "ok" : "bad";

  // Status is never colour alone -- the dots are decorative and the label
  // carries the meaning.
  const label = unconfigured
    ? "Not connected"
    : isError
      ? "API unreachable"
      : isPending
        ? "Checking…"
        : ch === "ok" && oa === "ok"
          ? "Connected"
          : "Degraded";

  return (
    <HoverCard openDelay={150}>
      <HoverCardTrigger asChild>
        <Link
          href="/settings/connection"
          className="flex items-center gap-2 rounded-[4px] px-1 py-1 text-[12px] text-ink-secondary hover:text-ink-primary"
        >
          <span className="flex gap-1">
            <Dot state={ch} />
            <Dot state={oa} />
          </span>
          {label}
        </Link>
      </HoverCardTrigger>

      <HoverCardContent side="top" align="start" className="w-72 text-[13px]">
        <dl className="space-y-2">
          <div>
            <dt className="label-caps text-ink-secondary">ClickHouse</dt>
            <dd className="cite text-ink-primary">
              {unconfigured
                ? "No connection configured"
                : isError
                ? "API unreachable"
                : isPending
                  ? "…"
                  : data.clickhouse.ok
                    ? `${data.clickhouse.database} · ${data.clickhouse.table_count} tables · v${data.clickhouse.version}`
                    : data.clickhouse.error}
            </dd>
          </div>
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
