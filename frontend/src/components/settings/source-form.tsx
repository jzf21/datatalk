"use client";

import { useState } from "react";
import { CheckCircle2, CircleAlert } from "lucide-react";

import type { ConnectionInput, ConnectionPublic } from "@/lib/api/auth";
import type { DataSource, DataSourceField } from "@/lib/connections/sources";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

export interface TestOutcome {
  ok: boolean;
  message: string;
}

/** `^[a-z][a-z0-9_]{0,39}$`, matching the API's validation exactly. */
const NAME_RE = /^[a-z][a-z0-9_]{0,39}$/;

function seed(
  source: DataSource,
  existing: ConnectionPublic | null,
): ConnectionInput {
  if (existing) {
    return {
      type: source.id,
      name: String(existing.name ?? "default"),
      description: String(existing.description ?? ""),
      is_default: Boolean(existing.is_default),
      host: String(existing.host ?? ""),
      port: Number(existing.port ?? source.defaults.port),
      user: String(existing.user ?? source.defaults.user),
      // The API never returns the stored password, and sending null keeps it.
      password: "",
      database: String(existing.database ?? source.defaults.database),
      secure: Boolean(existing.secure),
      sslmode: (existing.sslmode as ConnectionInput["sslmode"]) ?? null,
      // Not edited here -- the scope has its own screen -- but carried so that
      // saving a credential change sends it back unchanged. The API takes whole
      // connections, so omitting these would silently clear the scope.
      introspect_databases: (existing.introspect_databases as string[]) ?? [],
      introspect_tables: (existing.introspect_tables as string[]) ?? [],
      scope_query: existing.scope_query ?? "",
    };
  }
  return {
    type: source.id,
    name: "",
    description: "",
    is_default: false,
    introspect_databases: [],
    introspect_tables: [],
    ...source.defaults,
  };
}

/**
 * Add or edit one data source.
 *
 * `name` and `description` are not decoration: the name is the handle the agent
 * types in `run_sql(source: ...)`, and the description is what tells it which
 * source answers which question. Both are shown first for that reason.
 */
export function SourceForm({
  source,
  existing,
  canEdit,
  busy,
  result,
  onCancel,
  onTest,
  onSave,
}: {
  source: DataSource;
  existing: ConnectionPublic | null;
  canEdit: boolean;
  busy: "test" | "save" | null;
  result: TestOutcome | null;
  onCancel: () => void;
  onTest: (input: ConnectionInput) => void;
  onSave: (input: ConnectionInput) => void;
}) {
  const [form, setForm] = useState<ConnectionInput>(() =>
    seed(source, existing),
  );

  const set = <K extends keyof ConnectionInput>(
    key: K,
    value: ConnectionInput[K],
  ) => setForm((f) => ({ ...f, [key]: value }));

  const nameValid = NAME_RE.test(form.name);
  // Every field the engine shows is required unless it says otherwise -- a
  // Jira source without an account email fails at Jira, not here.
  const missing = source.fields.find(
    (f) =>
      !f.optional &&
      f.type !== "password" &&
      f.type !== "select" &&
      !String(form[f.id] ?? "").trim(),
  );
  const ready = canEdit && busy === null && !missing && nameValid;

  // A disabled Test/Save button is otherwise a dead end -- say what it's waiting
  // for so nobody has to guess which field is holding it back.
  const blockReason = !canEdit
    ? "Only an owner or admin can change data sources."
    : !form.name.trim()
      ? "Enter a source name to continue."
      : !nameValid
        ? "Source name must be lowercase letters, digits and underscores, starting with a letter."
        : missing
          ? `Enter ${missing.label.toLowerCase()} to continue.`
          : null;

  const payload = (): ConnectionInput => ({
    ...form,
    // Blank means "keep what's stored"; the UI never holds the real one.
    password: form.password ? form.password : null,
  });

  const renderField = (f: DataSourceField) => {
    const hint =
      f.id === "password" && existing?.has_password
        ? "Leave blank to keep the stored password."
        : f.hint;

    return (
      <div
        key={f.id}
        className={f.type === "textarea" ? "sm:col-span-2" : undefined}
      >
        <Label htmlFor={f.id} className="label-caps text-ink-secondary">
          {f.label}
        </Label>
        {f.type === "textarea" ? (
          <Textarea
            id={f.id}
            rows={2}
            disabled={!canEdit}
            value={String(form[f.id] ?? "")}
            placeholder={f.placeholder}
            onChange={(e) =>
              set(f.id, e.target.value as ConnectionInput[typeof f.id])
            }
            className="mt-1.5 bg-muted font-mono text-[12px]"
          />
        ) : f.type === "select" ? (
          <select
            id={f.id}
            disabled={!canEdit}
            value={String(form[f.id] ?? "")}
            onChange={(e) =>
              set(
                f.id,
                (e.target.value || null) as ConnectionInput[typeof f.id],
              )
            }
            className="mt-1.5 h-9 w-full rounded-[6px] border border-border bg-muted px-3 text-[13px] text-ink-primary disabled:opacity-60"
          >
            <option value="">(default)</option>
            {f.options?.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
        ) : (
          <Input
            id={f.id}
            type={f.type ?? "text"}
            disabled={!canEdit}
            placeholder={f.placeholder}
            value={String(form[f.id] ?? "")}
            onChange={(e) =>
              set(
                f.id,
                (f.type === "number"
                  ? Number(e.target.value)
                  : e.target.value) as ConnectionInput[typeof f.id],
              )
            }
            className="mt-1.5 bg-muted"
          />
        )}
        {hint && <p className="mt-1 text-[12px] text-ink-tertiary">{hint}</p>}
      </div>
    );
  };

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <Label htmlFor="name" className="label-caps text-ink-secondary">
            Source name
          </Label>
          <Input
            id="name"
            disabled={!canEdit}
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
            placeholder="analytics"
            className="mt-1.5 bg-muted"
          />
          <p className="mt-1 text-[12px] text-ink-tertiary">
            {form.name && !nameValid
              ? "Lowercase letters, digits and underscores; must start with a letter."
              : "How the agent refers to this source in its queries."}
          </p>
        </div>
        <div className="flex items-end pb-6">
          <div className="flex items-center gap-2">
            <Switch
              id="is_default"
              disabled={!canEdit}
              checked={form.is_default}
              onCheckedChange={(v) => set("is_default", v)}
            />
            <Label
              htmlFor="is_default"
              className="text-[13px] text-ink-secondary"
            >
              Default source
            </Label>
          </div>
        </div>
      </div>

      <div>
        <Label htmlFor="description" className="label-caps text-ink-secondary">
          What&apos;s in it
        </Label>
        <Textarea
          id="description"
          rows={2}
          disabled={!canEdit}
          value={form.description}
          onChange={(e) => set("description", e.target.value)}
          placeholder={
            source.synced
              ? "Leave empty for a sensible default, or name the teams and projects."
              : "Product events and sessions since 2023."
          }
          className="mt-1.5 bg-muted"
        />
        <p className="mt-1 text-[12px] text-ink-tertiary">
          Shown to the agent. A clear sentence here is what makes it pick this
          source for the right questions.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {source.fields.map(renderField)}
      </div>

      {!source.synced && (
        <div className="flex items-center gap-2">
          <Switch
            id="secure"
            disabled={!canEdit}
            checked={form.secure}
            onCheckedChange={(v) => set("secure", v)}
          />
          <Label htmlFor="secure" className="text-[13px] text-ink-secondary">
            {source.secureLabel}
          </Label>
        </div>
      )}

      {result && (
        <p
          role="status"
          className={`flex items-start gap-2 border-l-2 py-1 pl-3 text-[13px] text-ink-secondary ${
            result.ok ? "border-[var(--status-good)]" : "border-destructive"
          }`}
        >
          {result.ok ? (
            <CheckCircle2
              className="mt-0.5 size-3.5 shrink-0 text-status-good-text"
              aria-hidden
            />
          ) : (
            <CircleAlert
              className="mt-0.5 size-3.5 shrink-0 text-destructive"
              aria-hidden
            />
          )}
          <span className="cite">{result.message}</span>
        </p>
      )}

      <div className="space-y-2">
        {blockReason && !result && (
          <p className="text-[12px] text-ink-tertiary">{blockReason}</p>
        )}
        <div className="flex items-center gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={busy !== null}>
            Cancel
          </Button>
          <Button
            variant="outline"
            disabled={!ready}
            onClick={() => onTest(payload())}
          >
            {busy === "test" ? "Testing…" : "Test connection"}
          </Button>
          <Button disabled={!ready} onClick={() => onSave(payload())}>
            {busy === "save"
              ? "Saving…"
              : existing
                ? "Save changes"
                : "Add source"}
          </Button>
        </div>
      </div>
    </div>
  );
}
