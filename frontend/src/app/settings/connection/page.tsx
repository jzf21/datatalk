"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Database, ListFilter, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { useSession } from "@/components/auth/session-gate";
import { SourcePicker } from "@/components/settings/source-picker";
import {
  SourceForm,
  type TestOutcome,
} from "@/components/settings/source-form";
import { getDataSource, type DataSource } from "@/lib/connections/sources";
import {
  createConnection,
  deleteConnection,
  listConnections,
  testConnection,
  updateConnection,
  type ConnectionInput,
  type ConnectionPublic,
} from "@/lib/api/auth";
import { parseScope, scopeSummary } from "@/lib/connections/scope";
import { ApiError } from "@/lib/api/client";
import { qk } from "@/lib/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

/** Which editor is open: nothing, a new source of some type, or an existing one. */
type Editing =
  | { mode: "none" }
  | { mode: "picking" }
  | { mode: "new"; source: DataSource }
  | { mode: "edit"; source: DataSource; connection: ConnectionPublic };

export default function ConnectionPage() {
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

  const [editing, setEditing] = useState<Editing>({ mode: "none" });
  const [result, setResult] = useState<TestOutcome | null>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);

  const close = () => {
    setEditing({ mode: "none" });
    setResult(null);
  };

  /** Every source feeds the catalog, so health and schema are downstream of all of them. */
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["connections", orgId] });
    qc.invalidateQueries({ queryKey: qk.health });
    qc.invalidateQueries({ queryKey: qk.schema });
    // The context model names sources in its `covers` entries, so a rename
    // changes which file the agent is shown for a table.
    qc.invalidateQueries({ queryKey: qk.context(orgId) });
  };

  const onTest = async (input: ConnectionInput) => {
    if (!orgId) return;
    setBusy("test");
    setResult(null);
    try {
      const res = await testConnection(orgId, input);
      setResult({
        ok: res.ok,
        message: res.ok
          ? `Connected — ${res.table_count ?? "?"} tables in ${input.database}.`
          : String(res.error ?? "Connection failed."),
      });
    } catch (err) {
      setResult({
        ok: false,
        message: err instanceof ApiError ? err.message : "Couldn't reach the API.",
      });
    } finally {
      setBusy(null);
    }
  };

  const onSave = async (input: ConnectionInput) => {
    if (!orgId) return;
    setBusy("save");
    setResult(null);
    try {
      if (editing.mode === "edit") {
        await updateConnection(orgId, editing.connection.id, input);
        toast.success(`Saved ${input.name}`);
      } else {
        await createConnection(orgId, input);
        toast.success(`Added ${input.name}`);
      }
      invalidate();
      close();
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : "Couldn't reach the API.",
      );
    } finally {
      setBusy(null);
    }
  };

  const remove = useMutation({
    mutationFn: (c: ConnectionPublic) => deleteConnection(orgId!, c.id),
    onSuccess: (_data, c) => {
      toast.success(`Removed ${c.name}`);
      invalidate();
    },
    onError: (err) =>
      toast.error(
        err instanceof ApiError ? err.message : "Couldn't reach the API.",
      ),
  });

  const editorSource =
    editing.mode === "new" || editing.mode === "edit" ? editing.source : null;

  return (
    <>
      <PageHeader
        title="Data sources"
        meta="The warehouses this workspace reads from. Reports can draw on more than one."
      />
      <PageBody className="max-w-[640px] space-y-6">
        {isPending ? (
          <div className="space-y-3">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
        ) : editorSource ? (
          <>
            <p className="text-[13px] text-ink-secondary">
              {editing.mode === "edit" ? "Editing" : "New"}{" "}
              <span className="font-medium text-ink-primary">
                {editorSource.name}
              </span>{" "}
              source
            </p>
            <SourceForm
              // Remount on target change so the form reseeds from the new row.
              key={
                editing.mode === "edit" ? editing.connection.id : editorSource.id
              }
              source={editorSource}
              existing={editing.mode === "edit" ? editing.connection : null}
              canEdit={canEdit}
              busy={busy}
              result={result}
              onCancel={close}
              onTest={onTest}
              onSave={onSave}
            />
          </>
        ) : editing.mode === "picking" ? (
          <div className="space-y-3">
            <p className="text-[13px] text-ink-secondary">
              Choose an engine, then enter its connection details.
            </p>
            <SourcePicker
              disabled={!canEdit}
              onSelect={(source) => {
                setResult(null);
                setEditing({ mode: "new", source });
              }}
            />
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
          </div>
        ) : (
          <>
            {!canEdit && (
              <p className="rounded-[6px] border border-border bg-card p-3 text-[13px] text-ink-secondary">
                Only an owner or admin can change data sources.
              </p>
            )}

            {connections.length === 0 ? (
              <p className="text-[13px] text-ink-secondary">
                No data sources yet. Add one to start generating reports.
              </p>
            ) : (
              <ul className="space-y-2">
                {connections.map((c) => {
                  const source = getDataSource(c.type);
                  return (
                    <li
                      key={c.id}
                      className="flex items-start gap-3 rounded-[6px] border border-border bg-card p-3"
                    >
                      <Database
                        className="mt-0.5 size-4 shrink-0 text-ink-tertiary"
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-[14px] font-medium text-ink-primary">
                            {c.name}
                          </span>
                          <Badge variant="secondary">
                            {source?.name ?? c.type}
                          </Badge>
                          {c.is_default && <Badge>Default</Badge>}
                        </div>
                        <p className="mt-0.5 truncate text-[12px] text-ink-tertiary">
                          {c.user}@{c.host}:{c.port}/{c.database}
                          {" · "}
                          {scopeSummary(
                            parseScope(
                              c.introspect_databases as string[] | undefined,
                              c.introspect_tables as string[] | undefined,
                            ),
                          )}
                        </p>
                        {c.description && (
                          <p className="mt-1 text-[12px] text-ink-secondary">
                            {c.description}
                          </p>
                        )}
                      </div>
                      {canEdit && source && (
                        <div className="flex shrink-0 items-center gap-1">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => {
                              setResult(null);
                              setEditing({ mode: "edit", source, connection: c });
                            }}
                          >
                            Edit
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            aria-label={`Remove ${c.name}`}
                            disabled={remove.isPending}
                            onClick={() => remove.mutate(c)}
                          >
                            <Trash2 className="size-3.5" aria-hidden />
                          </Button>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}

            {canEdit && (
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="outline"
                  onClick={() => setEditing({ mode: "picking" })}
                >
                  <Plus className="size-3.5" aria-hidden />
                  Add source
                </Button>
                {connections.length > 0 && (
                  <>
                    <Button variant="ghost" asChild>
                      <Link href="/settings/scope">
                        <ListFilter className="size-3.5" aria-hidden />
                        Choose what it sees
                      </Link>
                    </Button>
                    <Button variant="ghost" asChild>
                      <Link href="/settings/context">
                        <BookOpen className="size-3.5" aria-hidden />
                        Document this data
                      </Link>
                    </Button>
                  </>
                )}
              </div>
            )}
          </>
        )}
      </PageBody>
    </>
  );
}
