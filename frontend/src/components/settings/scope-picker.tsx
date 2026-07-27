"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronRight, RefreshCw } from "lucide-react";

import type { DiscoveredNamespace } from "@/lib/api/auth";
import type { DataSource } from "@/lib/connections/sources";
import {
  ALL,
  isTableSelected,
  isUnscoped,
  namespaceState,
  toggleTable,
  withNamespace,
  type Scope,
} from "@/lib/connections/scope";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

/** Native inputs rather than the shadcn Checkbox: this tree needs a real
 * indeterminate state for a partially-selected database, which is a DOM
 * property and not something an attribute can express. */
function TriStateBox({
  state,
  disabled,
  label,
  onToggle,
}: {
  state: "checked" | "partial" | "unchecked";
  disabled: boolean;
  label: string;
  onToggle: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = state === "partial";
  }, [state]);

  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      disabled={disabled}
      checked={state === "checked"}
      onChange={onToggle}
      className="size-3.5 shrink-0 accent-primary disabled:opacity-50"
    />
  );
}

function rowCount(rows: number | null): string {
  if (rows === null) return "";
  return rows >= 1000
    ? `${Math.round(rows / 1000).toLocaleString()}k rows`
    : `${rows} rows`;
}

/**
 * Choose which databases, and which tables inside them, this source exposes.
 *
 * This is not a performance setting. Everything selected here is introspected
 * into `build_catalog()`, which lands in every agent prompt and drives context
 * generation -- so an unscoped server full of staging and backup databases is
 * what the agent reasons over. Narrowing here is what makes the catalog small
 * enough to be read carefully.
 *
 * Browsing is deliberately live and unscoped: the list has to show what the
 * current scope excludes, or it could only ever narrow and never widen.
 */
export function ScopePicker({
  source,
  scope,
  canEdit,
  connectionKey,
  onScopeChange,
  onDiscover,
}: {
  source: DataSource;
  scope: Scope;
  canEdit: boolean;
  /** Credentials the discovered list belongs to; changing it invalidates. */
  connectionKey: string;
  onScopeChange: (next: Scope) => void;
  onDiscover: () => Promise<{
    ok: boolean;
    error?: string;
    truncated?: boolean;
    databases?: DiscoveredNamespace[];
  }>;
}) {
  const [result, setResult] = useState<{
    key: string;
    namespaces?: DiscoveredNamespace[];
    truncated?: boolean;
    error?: string;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());

  // On Postgres one connection is one database, so the schema list is only
  // valid for the credentials above it -- editing `database` must not leave a
  // stale tree that would save selections against the wrong server. Tagging
  // the result with the credentials it came from expires it by derivation,
  // rather than by an effect that resets state a render too late.
  const fresh = result?.key === connectionKey ? result : null;
  const namespaces = fresh?.namespaces ?? null;
  const truncated = Boolean(fresh?.truncated);
  const error = fresh?.error ?? null;

  const browse = async () => {
    setLoading(true);
    const key = connectionKey;
    try {
      const res = await onDiscover();
      setResult(
        res.ok
          ? { key, namespaces: res.databases ?? [], truncated: Boolean(res.truncated) }
          : { key, error: res.error ?? "Couldn't read the server." },
      );
    } catch {
      setResult({ key, error: "Couldn't reach the API." });
    } finally {
      setLoading(false);
    }
  };

  const label = source.namespaceLabel;

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="label-caps text-ink-secondary">{label} in scope</span>
        <Button
          variant="outline"
          size="sm"
          disabled={!canEdit || loading}
          onClick={browse}
        >
          <RefreshCw
            className={cn("size-3.5", loading && "animate-spin")}
            aria-hidden
          />
          {namespaces === null ? "Browse server" : "Refresh"}
        </Button>
      </div>

      <p className="mt-1 text-[12px] text-ink-tertiary">
        Everything selected here goes into every agent prompt. Leave it empty to
        expose the whole server.
      </p>

      {error && (
        <p role="status" className="mt-2 text-[12px] text-destructive">
          {error}
        </p>
      )}

      {namespaces === null ? (
        <SavedScope scope={scope} label={label} />
      ) : namespaces.length === 0 ? (
        <p className="mt-3 text-[13px] text-ink-secondary">
          No {label.toLowerCase()} visible to this user.
        </p>
      ) : (
        <div className="mt-3 max-h-[320px] overflow-y-auto rounded-[6px] border border-border bg-card">
          <ul className="divide-y divide-border">
            {namespaces.map((ns) => {
              const state = namespaceState(scope, ns.name);
              const expanded = open.has(ns.name);
              const tableNames = ns.tables.map((t) => t.name);
              return (
                <li key={ns.name}>
                  <div className="flex items-center gap-2 px-3 py-2">
                    <TriStateBox
                      state={state}
                      disabled={!canEdit}
                      label={`Include ${ns.name}`}
                      onToggle={() =>
                        onScopeChange(
                          withNamespace(
                            scope,
                            ns.name,
                            state === "unchecked" ? ALL : null,
                          ),
                        )
                      }
                    />
                    <button
                      type="button"
                      onClick={() =>
                        setOpen((prev) => {
                          const next = new Set(prev);
                          if (next.has(ns.name)) next.delete(ns.name);
                          else next.add(ns.name);
                          return next;
                        })
                      }
                      aria-expanded={expanded}
                      className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-[13px] text-ink-primary"
                    >
                      <ChevronRight
                        className={cn(
                          "size-3.5 shrink-0 text-ink-tertiary transition-transform duration-[120ms]",
                          expanded && "rotate-90",
                        )}
                        aria-hidden
                      />
                      <span className="truncate">{ns.name}</span>
                      <span className="shrink-0 text-[12px] text-ink-tertiary">
                        {state === "partial"
                          ? `${(scope.get(ns.name) as ReadonlySet<string>).size} of ${ns.tables.length} tables`
                          : `${ns.tables.length} tables`}
                      </span>
                    </button>
                  </div>

                  {expanded && (
                    <ul className="border-t border-border bg-muted/40 py-1 pl-9 pr-3">
                      {ns.tables.map((t) => (
                        <li
                          key={t.name}
                          className="flex items-center gap-2 py-1"
                        >
                          <TriStateBox
                            state={
                              isTableSelected(scope, ns.name, t.name)
                                ? "checked"
                                : "unchecked"
                            }
                            disabled={!canEdit}
                            label={`Include ${ns.name}.${t.name}`}
                            onToggle={() =>
                              onScopeChange(
                                toggleTable(scope, ns.name, t.name, tableNames),
                              )
                            }
                          />
                          <span className="min-w-0 flex-1 truncate text-[13px] text-ink-secondary">
                            {t.name}
                          </span>
                          <span className="shrink-0 text-[12px] text-ink-tertiary">
                            {rowCount(t.rows)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {truncated && (
        <p className="mt-2 text-[12px] text-ink-tertiary">
          This server has more tables than the browser lists. Anything not shown
          can still be reached by selecting its whole {label.toLowerCase().replace(/s$/, "")}.
        </p>
      )}
    </div>
  );
}

/**
 * What is saved, before anyone has browsed.
 *
 * Editing a source should not require a live round trip just to see its scope
 * -- and a server that is down is exactly when someone comes here to look.
 */
function SavedScope({ scope, label }: { scope: Scope; label: string }) {
  if (isUnscoped(scope)) {
    return (
      <p className="mt-3 text-[13px] text-ink-secondary">
        Every {label.toLowerCase().replace(/s$/, "")} on this server is in scope.
      </p>
    );
  }

  return (
    <ul className="mt-3 space-y-1 rounded-[6px] border border-border bg-card p-3">
      {[...scope.entries()].map(([namespace, selection]) => (
        <li key={namespace} className="text-[13px] text-ink-secondary">
          <span className="text-ink-primary">{namespace}</span>
          {selection === ALL
            ? " — all tables"
            : ` — ${[...selection].sort().join(", ")}`}
        </li>
      ))}
    </ul>
  );
}
