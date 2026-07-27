"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Cable, Database } from "lucide-react";
import { toast } from "sonner";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { useSession } from "@/components/auth/session-gate";
import { ScopePicker } from "@/components/settings/scope-picker";
import {
  getDataSource,
  toConnectionInput,
  sourceTypeName,
} from "@/lib/connections/sources";
import {
  parseScope,
  scopeSummary,
  toScopeArrays,
  type Scope,
} from "@/lib/connections/scope";
import {
  discoverConnection,
  listConnections,
  updateConnection,
  type ConnectionPublic,
} from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { qk } from "@/lib/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * What the agent is allowed to see, per source.
 *
 * Its own screen rather than a section of the connection form: scope is the
 * setting people come back to, long after the credentials are settled, and it
 * is the one that decides how much of the warehouse lands in every prompt.
 * Editing it should not mean reopening a form full of hostnames and passwords.
 */
export default function ScopePage() {
  const { org } = useSession();
  const qc = useQueryClient();
  const orgId = org?.id ?? null;
  const canEdit = org?.role === "owner" || org?.role === "admin";

  const { data, isPending } = useQuery({
    queryKey: ["connections", orgId],
    queryFn: () => listConnections(orgId!),
    enabled: orgId !== null,
    retry: false,
  });
  const connections = data?.connections ?? [];

  return (
    <>
      <PageHeader
        title="Data scope"
        meta="Which databases and tables each source exposes to the agent. Everything in scope goes into every prompt."
      />
      <PageBody className="max-w-[720px] space-y-6">
        {!canEdit && (
          <p className="rounded-[6px] border border-border bg-card p-3 text-[13px] text-ink-secondary">
            Only an owner or admin can change what a source exposes.
          </p>
        )}

        {isPending ? (
          <div className="space-y-3">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-32 w-full" />
            ))}
          </div>
        ) : connections.length === 0 ? (
          <div className="space-y-3">
            <p className="text-[13px] text-ink-secondary">
              No data sources yet. There is nothing to scope until this
              workspace connects to a warehouse.
            </p>
            <Button variant="outline" asChild>
              <Link href="/settings/connection">
                <Cable className="size-3.5" aria-hidden />
                Add a data source
              </Link>
            </Button>
          </div>
        ) : (
          connections.map((connection) => (
            <SourceScope
              key={connection.id}
              connection={connection}
              orgId={orgId!}
              canEdit={canEdit}
              onSaved={() => {
                qc.invalidateQueries({ queryKey: ["connections", orgId] });
                qc.invalidateQueries({ queryKey: qk.schema });
                // The catalog every agent sees just changed shape, and the
                // context model's `covers` entries point at tables that may no
                // longer be in scope.
                qc.invalidateQueries({ queryKey: qk.context(orgId) });
              }}
            />
          ))
        )}
      </PageBody>
    </>
  );
}

function SourceScope({
  connection,
  orgId,
  canEdit,
  onSaved,
}: {
  connection: ConnectionPublic;
  orgId: string;
  canEdit: boolean;
  onSaved: () => void;
}) {
  const stored = parseScope(
    connection.introspect_databases,
    connection.introspect_tables,
  );
  // null means "not edited yet", which is what keeps a refetch from clobbering
  // an in-progress selection.
  const [draft, setDraft] = useState<Scope | null>(null);
  const [saving, setSaving] = useState(false);

  const scope = draft ?? stored;
  const next = toScopeArrays(scope);
  const dirty =
    draft !== null &&
    (next.introspect_databases.join() !==
      [...(connection.introspect_databases ?? [])].sort().join() ||
      next.introspect_tables.join() !==
        [...(connection.introspect_tables ?? [])].sort().join());

  const source = getDataSource(connection.type);

  const save = async () => {
    setSaving(true);
    try {
      await updateConnection(
        orgId,
        connection.id,
        toConnectionInput(connection, next),
      );
      toast.success(`Saved scope for ${connection.name}`);
      setDraft(null);
      onSaved();
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : "Couldn't reach the API.",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="rounded-[6px] border border-border bg-card p-4">
      <div className="flex flex-wrap items-start gap-3">
        <Database
          className="mt-0.5 size-4 shrink-0 text-ink-tertiary"
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[14px] font-medium text-ink-primary">
              {connection.name}
            </span>
            <Badge variant="secondary">{sourceTypeName(connection.type)}</Badge>
            {connection.is_default && <Badge>Default</Badge>}
          </div>
          <p className="mt-0.5 truncate text-[12px] text-ink-tertiary">
            {connection.user}@{connection.host}:{connection.port}/
            {connection.database} · {scopeSummary(stored)}
          </p>
        </div>
      </div>

      <div className="mt-4">
        {source === null ? (
          <p className="text-[13px] text-ink-secondary">
            This source uses an engine this build does not know
            ({connection.type}), so its scope cannot be browsed here.
          </p>
        ) : (
          <ScopePicker
            source={source}
            scope={scope}
            canEdit={canEdit}
            // Scope is edited against a saved source, so the credentials cannot
            // change underneath it -- the id alone identifies the server.
            connectionKey={connection.id}
            onScopeChange={setDraft}
            onDiscover={() =>
              discoverConnection(orgId, toConnectionInput(connection))
            }
          />
        )}
      </div>

      {dirty && (
        <div className="mt-4 flex items-center gap-2 border-t border-border pt-3">
          <Button disabled={saving} onClick={save}>
            {saving ? "Saving…" : "Save scope"}
          </Button>
          <Button
            variant="ghost"
            disabled={saving}
            onClick={() => setDraft(null)}
          >
            Discard
          </Button>
          <span className="text-[12px] text-ink-tertiary">
            {scopeSummary(scope)} — not saved yet
          </span>
        </div>
      )}
    </section>
  );
}
