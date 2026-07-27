"use client";

import { useState } from "react";
import { Eye, Pencil } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Markdown } from "@/components/markdown/markdown";
import type { ContextFileDetail } from "@/lib/api/datacontext";

/**
 * One file, edited as markdown.
 *
 * Per-file rather than one textarea over the whole model: parsing an edited
 * monolith back into files means a renamed heading silently orphans an entity's
 * documentation.
 */
export function ContextEditor({
  file,
  canEdit,
  saving,
  onSave,
  onDelete,
}: {
  file: ContextFileDetail;
  canEdit: boolean;
  saving: boolean;
  onSave: (body: string, summary: string) => void;
  onDelete: () => void;
}) {
  // Seeded once. The parent passes `key={file.path}`, so selecting another file
  // remounts this component and reseeds from the new row -- the same trick
  // SourceForm uses, and the reason there is no effect syncing props to state.
  const [body, setBody] = useState(file.body_md);
  const [summary, setSummary] = useState(file.summary);
  const [preview, setPreview] = useState(false);

  // A viewer with no edit rights only ever reads, so there is nothing to toggle:
  // they get the rendered markdown, never the raw textarea. The Edit/Preview
  // switch exists solely for someone who can change the body.
  const rendered = preview || !canEdit;

  const dirty = body !== file.body_md || summary !== file.summary;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <code className="cite text-[13px]">{file.path}</code>
        <Badge variant="secondary">{file.origin}</Badge>
        {file.human_owned && <Badge>Edited</Badge>}
        <span className="flex-1" />
        {canEdit && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPreview((p) => !p)}
            aria-pressed={preview}
          >
            {preview ? (
              <>
                <Pencil className="size-3.5" aria-hidden /> Edit
              </>
            ) : (
              <>
                <Eye className="size-3.5" aria-hidden /> Preview
              </>
            )}
          </Button>
        )}
      </div>

      <div className="space-y-1.5">
        <label
          htmlFor="context-summary"
          className="label-caps block text-ink-tertiary"
        >
          Summary
        </label>
        <Input
          id="context-summary"
          value={summary}
          maxLength={200}
          disabled={!canEdit}
          onChange={(e) => setSummary(e.target.value)}
        />
        <p className="text-[12px] text-ink-tertiary">
          This one line goes into every prompt the workspace sends. Say what the
          file is for, not what it is called.
        </p>
      </div>

      {rendered ? (
        <div className="rounded-[6px] border border-border bg-card p-4">
          <Markdown className="doc-measure">{body}</Markdown>
        </div>
      ) : (
        <Textarea
          aria-label={`${file.path} contents`}
          value={body}
          maxLength={16000}
          disabled={!canEdit}
          onChange={(e) => setBody(e.target.value)}
          className="min-h-[420px] font-mono text-[13px] leading-relaxed"
        />
      )}

      {canEdit && (
        <div className="flex items-center gap-2">
          <Button onClick={() => onSave(body, summary)} disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
          {dirty && (
            <span className="text-[12px] text-ink-tertiary">Unsaved changes</span>
          )}
          <span className="flex-1" />
          <Button variant="ghost" size="sm" onClick={onDelete} disabled={saving}>
            Delete file
          </Button>
        </div>
      )}
    </div>
  );
}
