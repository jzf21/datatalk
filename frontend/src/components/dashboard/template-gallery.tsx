"use client";

import { ArrowRight, Loader2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { ApiError, apiErrorMessage } from "@/lib/api/client";
import {
  useCreateDashboardFromTemplate,
  useDashboardTemplates,
} from "@/lib/api/queries";
import type { ReportTemplateSummary } from "@/lib/api/types";

const FAMILY_LABEL: Record<ReportTemplateSummary["family"], string> = {
  sprint: "Sprints",
  flow: "Flow",
  backlog: "Delivery",
  people: "People",
};

/**
 * Ready-made reports for sources whose schema DataTalk owns (synced Jira).
 *
 * Sits under the composer rather than beside it: the composer stays the one
 * elevated object on the page, and these are flat, hairline cards. Building one
 * runs its SQL once and routes straight to the live dashboard -- there is no
 * generation to watch.
 */
export function TemplateGallery() {
  const router = useRouter();
  const templates = useDashboardTemplates();
  const create = useCreateDashboardFromTemplate();
  const [pending, setPending] = useState<string | null>(null);

  const all = templates.data ?? [];
  const sources = Array.from(new Set(all.flatMap((t) => t.sources)));
  const [chosen, setChosen] = useState<string | null>(null);
  const source = chosen && sources.includes(chosen) ? chosen : sources[0];

  if (!all.length || !source) return null;

  const build = (t: ReportTemplateSummary) => {
    setPending(t.id);
    create.mutate(
      { template_id: t.id, source },
      {
        onSuccess: (res) => router.push(`/dashboards/${res.dashboard_id}`),
        onError: (err) => {
          setPending(null);
          toast.error(
            err instanceof ApiError
              ? apiErrorMessage(err.message)
              : "Couldn't reach the API.",
          );
        },
      },
    );
  };

  return (
    <section aria-labelledby="template-gallery-heading" className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3 border-b border-border pb-2">
        <h2
          id="template-gallery-heading"
          className="label-caps text-ink-tertiary"
        >
          Or start from a Jira report
        </h2>
        {sources.length > 1 && (
          <label className="flex items-center gap-2 text-[13px] text-ink-secondary">
            Source
            <select
              className="h-8 rounded-[4px] border border-border bg-transparent px-2 text-[13px]"
              value={source}
              onChange={(e) => setChosen(e.target.value)}
            >
              {sources.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <ul className="grid gap-3 sm:grid-cols-2">
        {all
          .filter((t) => t.sources.includes(source))
          .map((t) => (
            <li
              key={t.id}
              className="flex flex-col gap-3 rounded-[6px] border border-border bg-card p-4"
            >
              <div className="space-y-1">
                <p className="label-caps text-ink-tertiary">
                  {FAMILY_LABEL[t.family]}
                </p>
                <h3 className="text-[15px] font-medium text-ink-primary">
                  {t.title}
                </h3>
                <p className="text-[13px] leading-relaxed text-ink-secondary">
                  {t.description}
                </p>
              </div>
              <p className="text-[12px] text-ink-tertiary">
                Filter by {t.filters.join(", ").toLowerCase()}
              </p>
              <div className="mt-auto">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={pending !== null}
                  onClick={() => build(t)}
                  aria-label={`Build ${t.title} from ${source}`}
                >
                  {pending === t.id ? (
                    <Loader2 className="size-3.5 animate-spin" />
                  ) : (
                    <ArrowRight className="size-3.5" />
                  )}
                  Build
                </Button>
              </div>
            </li>
          ))}
      </ul>
    </section>
  );
}
