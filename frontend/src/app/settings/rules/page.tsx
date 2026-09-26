"use client";

import { useState } from "react";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import {
  useAddSuggestion,
  useDeleteSuggestion,
  useSuggestions,
} from "@/lib/api/queries";
import { formatRelativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";

/** Clickable starters, because "what do I write here?" is the real blocker. */
const EXAMPLES = [
  "For SLA metrics use the `jira.sla_events` table, not `issues`.",
  "An account is at-risk when its breach rate is above 5%.",
  "Always exclude test accounts (`account_id LIKE 'test_%'`).",
];

export default function HouseRulesPage() {
  const [text, setText] = useState("");
  const suggestions = useSuggestions();
  const add = useAddSuggestion();
  const remove = useDeleteSuggestion();

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed) return;
    add.mutate(trimmed, {
      onSuccess: () => setText(""),
      onError: (err) => toast.error(String(err)),
    });
  };

  return (
    <>
      <PageHeader
        title="House rules"
        meta="Standing instructions retrieved and applied to every generation."
      />
      <PageBody className="max-w-[680px] space-y-8">
        <div>
          <Label htmlFor="rule" className="label-caps text-ink-secondary">
            Add a rule
          </Label>
          <Textarea
            id="rule"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
            }}
            rows={3}
            placeholder="Tell the agents something they can't infer from the schema."
            className="mt-1.5 resize-y bg-muted"
          />

          {!text && (
            <ul className="mt-2 flex flex-wrap gap-1.5">
              {EXAMPLES.map((example) => (
                <li key={example}>
                  <button
                    type="button"
                    onClick={() => setText(example)}
                    className="rounded-[4px] border border-border bg-card px-2 py-1 text-left text-[12px] text-ink-secondary hover:border-border-strong hover:text-ink-primary"
                  >
                    {example}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <Button
            className="mt-3"
            onClick={submit}
            disabled={!text.trim() || add.isPending}
          >
            {add.isPending ? "Saving…" : "Add rule"}
          </Button>
        </div>

        <section>
          <h2 className="label-caps mb-3 text-ink-tertiary">
            Current rules{suggestions.data && ` · ${suggestions.data.length}`}
          </h2>

          {suggestions.isPending && (
            <div className="space-y-2">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          )}

          {suggestions.isError && (
            <p className="text-[13px] text-ink-secondary">
              Couldn&rsquo;t load rules.
            </p>
          )}

          {suggestions.data?.length === 0 && (
            <p className="text-[13px] text-ink-secondary">
              No rules yet. They&rsquo;re retrieved by relevance, so a handful of
              specific ones beats a long list.
            </p>
          )}

          <ul className="divide-y divide-border rounded-[6px] border border-border bg-card empty:hidden">
            {suggestions.data?.map((rule) => (
              <li key={rule.id} className="flex items-start gap-3 px-3 py-2.5">
                <p className="min-w-0 flex-1 text-[13px] leading-snug text-ink-primary">
                  {rule.text}
                </p>
                <span className="shrink-0 text-[12px] text-ink-tertiary">
                  {formatRelativeTime(rule.created_at)}
                </span>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-6 shrink-0"
                  aria-label={`Delete rule: ${rule.text.slice(0, 40)}`}
                  onClick={() => remove.mutate(rule.id)}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </li>
            ))}
          </ul>
        </section>
      </PageBody>
    </>
  );
}
