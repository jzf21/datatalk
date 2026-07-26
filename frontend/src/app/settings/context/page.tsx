"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { toast } from "sonner";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { useSession } from "@/components/auth/session-gate";
import { ContextEditor } from "@/components/settings/context-editor";
import { ContextTree } from "@/components/settings/context-tree";
import { useContextRun } from "@/hooks/use-context-run";
import {
  deleteContextFile,
  getContext,
  getContextFile,
  updateContextFile,
} from "@/lib/api/datacontext";
import { ApiError } from "@/lib/api/client";
import { qk } from "@/lib/api/queries";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

const errorText = (err: unknown) =>
  err instanceof ApiError ? err.message : "Couldn't reach the API.";

export default function ContextPage() {
  const { org } = useSession();
  const qc = useQueryClient();
  const orgId = org?.id ?? null;
  const canEdit = org?.role === "owner" || org?.role === "admin";

  const [selected, setSelected] = useState<string | null>(null);
  const run = useContextRun();

  const { data, isPending } = useQuery({
    queryKey: qk.context(orgId),
    queryFn: ({ signal }) => getContext(orgId!, signal),
    enabled: orgId !== null,
    retry: false,
  });
  const files = data?.files ?? [];

  const { data: file } = useQuery({
    queryKey: qk.contextFile(orgId, selected ?? ""),
    queryFn: () => getContextFile(orgId!, selected!),
    enabled: orgId !== null && selected !== null,
    retry: false,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.context(orgId) });
    // The tree feeds every prompt, so the schema preview is downstream of it.
    qc.invalidateQueries({ queryKey: qk.schema });
  };

  const save = useMutation({
    mutationFn: ({ body, summary }: { body: string; summary: string }) =>
      updateContextFile(orgId!, selected!, { body_md: body, summary }),
    onSuccess: () => {
      toast.success(`Saved ${selected}`);
      invalidate();
      qc.invalidateQueries({ queryKey: qk.contextFile(orgId, selected ?? "") });
    },
    onError: (err) => toast.error(errorText(err)),
  });

  const remove = useMutation({
    mutationFn: () => deleteContextFile(orgId!, selected!),
    onSuccess: () => {
      toast.success(`Removed ${selected}`);
      setSelected(null);
      invalidate();
    },
    onError: (err) => toast.error(errorText(err)),
  });

  const generate = (mode: "revise" | "replace") => {
    if (!orgId) return;
    run.start(orgId, mode, mode === "replace", invalidate);
  };

  const hasContext = files.length > 0;

  return (
    <>
      <PageHeader
        title="Data context"
        meta={
          data?.doc?.generated_at
            ? `Generated with ${data.doc.model} · ${files.length} files`
            : "What your data means, in the agents' own reading list."
        }
        actions={
          canEdit && (
            <Button
              variant="outline"
              size="sm"
              disabled={run.running}
              onClick={() => generate(hasContext ? "revise" : "replace")}
            >
              <Sparkles className="size-3.5" aria-hidden />
              {run.running
                ? "Generating…"
                : hasContext
                  ? "Regenerate"
                  : "Generate"}
            </Button>
          )
        }
      />

      <PageBody className="space-y-6">
        <p className="doc-measure text-[13px] text-ink-secondary">
          The catalog tells the agents which columns exist. This tells them what
          those columns <em>mean</em>: how entities map onto tables, and how this
          workspace defines its metrics. Only each file&rsquo;s summary line goes
          into every prompt — the agents read the rest on demand.
        </p>

        {run.state.phase !== "idle" && (
          <GenerationProgress run={run} />
        )}

        {isPending ? (
          <div className="space-y-3">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
        ) : !hasContext ? (
          <EmptyState canEdit={canEdit} running={run.running} />
        ) : (
          <div className="flex flex-col gap-8 md:flex-row">
            <div className="shrink-0 md:w-[220px]">
              <ContextTree
                files={files}
                selected={selected}
                onSelect={setSelected}
              />
            </div>
            <div className="min-w-0 flex-1">
              {file && selected ? (
                <ContextEditor
                  key={file.path}
                  file={file}
                  canEdit={canEdit}
                  saving={save.isPending || remove.isPending}
                  onSave={(body, summary) => save.mutate({ body, summary })}
                  onDelete={() => remove.mutate()}
                />
              ) : (
                <p className="text-[13px] text-ink-secondary">
                  Select a file to read or edit it.
                </p>
              )}
            </div>
          </div>
        )}

        {data?.tree_preview && (
          <details className="border-t border-border pt-6">
            <summary className="cursor-pointer text-[13px] text-ink-secondary">
              What the agents actually see
            </summary>
            <pre className="mt-3 overflow-x-auto rounded-[6px] border border-border bg-card p-3 text-[12px] leading-relaxed text-ink-secondary">
              {data.tree_preview}
            </pre>
          </details>
        )}
      </PageBody>
    </>
  );
}

function EmptyState({
  canEdit,
  running,
}: {
  canEdit: boolean;
  running: boolean;
}) {
  return (
    <div className="rounded-[6px] border border-border bg-card p-4">
      <p className="text-[13px] text-ink-secondary">
        No context yet.{" "}
        {canEdit
          ? running
            ? "Generation is running — files appear as they are written."
            : "Generate reads your warehouses, profiles the data, and writes an ontology and playbooks you can then correct."
          : "Ask an owner or admin to generate it."}
      </p>
    </div>
  );
}

function GenerationProgress({ run }: { run: ReturnType<typeof useContextRun> }) {
  const { state } = run;
  return (
    <div className="space-y-3 rounded-[6px] border border-border bg-card p-4">
      <div className="flex items-center gap-3">
        {run.running && (
          <span className="h-0.5 flex-1 overflow-hidden rounded-full bg-muted">
            <span className="shimmer block h-full w-full" />
          </span>
        )}
        {run.running && (
          <Button variant="ghost" size="sm" onClick={run.stop}>
            Stop
          </Button>
        )}
        {!run.running && (
          <Button variant="ghost" size="sm" onClick={run.reset}>
            Dismiss
          </Button>
        )}
      </div>

      {/* Phase transitions only -- not every status line, which would flood. */}
      <p className="sr-only" role="status" aria-live="polite">
        {state.phase === "streaming"
          ? "Generating data context"
          : state.phase === "done"
            ? `Done. ${state.savedCount} files written.`
            : state.phase}
      </p>

      {state.status && (
        <p className="text-[13px] text-ink-secondary">{state.status}</p>
      )}

      {(state.entities.length > 0 || state.playbooks.length > 0) && (
        <p className="text-[12px] text-ink-tertiary">
          {state.entities.length} entities · {state.playbooks.length} playbooks
        </p>
      )}

      {state.files.length > 0 && (
        <ul role="log" aria-live="off" className="space-y-0.5 text-[12px]">
          {state.files.slice(-6).map((f) => (
            <li key={f.path} className="text-ink-secondary">
              <code className="cite">{f.path}</code>
              {f.revised ? " revised" : " written"}
            </li>
          ))}
        </ul>
      )}

      {state.warnings.length > 0 && (
        <ul className="space-y-0.5 text-[12px] text-ink-tertiary">
          {state.warnings.slice(-3).map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}

      {state.fatal && (
        <p
          role="alert"
          className="border-l-2 border-destructive py-1 pl-3 text-[13px] text-ink-secondary"
        >
          {state.fatal}
        </p>
      )}
    </div>
  );
}
