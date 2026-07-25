"use client";

import Link from "next/link";

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
  const { data, isPending, isError } = useHealth();

  const ch = isPending || isError ? "unknown" : data.clickhouse.ok ? "ok" : "bad";
  const oa = isPending || isError ? "unknown" : data.openai.ok ? "ok" : "bad";

  // Status is never colour alone -- the dots are decorative and the label
  // carries the meaning.
  const label = isError
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
              {isError
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
              {isError
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
