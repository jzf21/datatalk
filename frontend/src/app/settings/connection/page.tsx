"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert } from "lucide-react";
import { toast } from "sonner";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { useSession } from "@/components/auth/session-gate";
import {
  getConnection,
  putConnection,
  testConnection,
  type ConnectionInput,
} from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { qk } from "@/lib/api/queries";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";

const BLANK: ConnectionInput = {
  host: "",
  port: 8123,
  user: "default",
  password: "",
  database: "default",
  secure: false,
};

export default function ConnectionPage() {
  const { org } = useSession();
  const qc = useQueryClient();
  const orgId = org?.id ?? null;
  const canEdit = org?.role === "owner" || org?.role === "admin";

  const { data, isPending } = useQuery({
    queryKey: ["connection", orgId],
    queryFn: () => getConnection(orgId!),
    enabled: orgId !== null,
    retry: false,
  });

  const [form, setForm] = useState<ConnectionInput>(BLANK);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(
    null,
  );
  const [busy, setBusy] = useState<"test" | "save" | null>(null);

  useEffect(() => {
    if (!data || data.configured === false) return;
    setForm({
      host: String(data.host ?? ""),
      port: Number(data.port ?? 8123),
      user: String(data.user ?? data.username ?? "default"),
      // The API never returns the stored password, and sending null keeps it.
      password: "",
      database: String(data.database ?? "default"),
      secure: Boolean(data.secure),
    });
  }, [data]);

  const payload = (): ConnectionInput => ({
    ...form,
    password: form.password ? form.password : null,
  });

  const run = async (kind: "test" | "save") => {
    if (!orgId) return;
    setBusy(kind);
    setResult(null);
    try {
      if (kind === "test") {
        const res = await testConnection(orgId, payload());
        setResult({
          ok: res.ok,
          message: res.ok
            ? `Connected — ${res.table_count ?? "?"} tables in ${form.database}.`
            : String(res.error ?? "Connection failed."),
        });
      } else {
        await putConnection(orgId, payload());
        toast.success("Connection saved");
        setForm((f) => ({ ...f, password: "" }));
        // Health and schema are both downstream of the connection.
        qc.invalidateQueries({ queryKey: ["connection", orgId] });
        qc.invalidateQueries({ queryKey: qk.health });
        qc.invalidateQueries({ queryKey: qk.schema });
      }
    } catch (err) {
      const message =
        err instanceof ApiError ? err.message : "Couldn't reach the API.";
      if (kind === "test") setResult({ ok: false, message });
      else toast.error(message);
    } finally {
      setBusy(null);
    }
  };

  const field = (
    id: keyof ConnectionInput,
    label: string,
    type: "text" | "number" | "password" = "text",
    hint?: string,
  ) => (
    <div>
      <Label htmlFor={id} className="label-caps text-ink-secondary">
        {label}
      </Label>
      <Input
        id={id}
        type={type}
        disabled={!canEdit}
        value={String(form[id] ?? "")}
        onChange={(e) =>
          setForm((f) => ({
            ...f,
            [id]: type === "number" ? Number(e.target.value) : e.target.value,
          }))
        }
        className="mt-1.5 bg-muted"
      />
      {hint && <p className="mt-1 text-[12px] text-ink-tertiary">{hint}</p>}
    </div>
  );

  return (
    <>
      <PageHeader
        title="Connection"
        meta="The ClickHouse warehouse this workspace reads from."
      />
      <PageBody className="max-w-[560px] space-y-6">
        {isPending ? (
          <div className="space-y-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : (
          <>
            {!canEdit && (
              <p className="rounded-[6px] border border-border bg-card p-3 text-[13px] text-ink-secondary">
                Only an owner or admin can change the connection.
              </p>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
              {field("host", "Host")}
              {field("port", "Port", "number")}
              {field("user", "Username")}
              {field(
                "password",
                "Password",
                "password",
                data?.configured === false
                  ? undefined
                  : "Leave blank to keep the stored password.",
              )}
              {field("database", "Database")}
            </div>

            <div className="flex items-center gap-2">
              <Switch
                id="secure"
                disabled={!canEdit}
                checked={form.secure}
                onCheckedChange={(v) => setForm((f) => ({ ...f, secure: v }))}
              />
              <Label htmlFor="secure" className="text-[13px] text-ink-secondary">
                Use TLS (HTTPS)
              </Label>
            </div>

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

            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                disabled={!canEdit || busy !== null || !form.host}
                onClick={() => run("test")}
              >
                {busy === "test" ? "Testing…" : "Test connection"}
              </Button>
              <Button
                disabled={!canEdit || busy !== null || !form.host}
                onClick={() => run("save")}
              >
                {busy === "save" ? "Saving…" : "Save"}
              </Button>
            </div>
          </>
        )}
      </PageBody>
    </>
  );
}
