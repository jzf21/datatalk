"use client";

import { useState } from "react";
import Link from "next/link";
import { Plus, Search } from "lucide-react";

import { PageBody, PageHeader } from "@/components/layout/page-header";
import { groupByDay, matchesQuery } from "@/lib/library";
import { formatRelativeTime } from "@/lib/format";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";

export interface LibraryItem {
  id: number;
  href: string;
  title: string;
  createdAt: string;
}

/**
 * The index page for one kind of run (reports or dashboards).
 *
 * Dense ledger lines under day headings -- the same grammar as the sidebar's
 * Recent rail -- with a search box, and an empty state shaped like the
 * composer's invitation: a serif headline, one sentence, one action.
 */
export function LibraryIndex({
  title,
  noun,
  newHref,
  newLabel,
  emptyHeadline,
  emptyBody,
  items,
  isPending,
  isError,
}: {
  title: string;
  /** Plural, lower case: "reports". */
  noun: string;
  newHref: string;
  newLabel: string;
  emptyHeadline: string;
  emptyBody: string;
  items: LibraryItem[];
  isPending: boolean;
  isError: boolean;
}) {
  const [query, setQuery] = useState("");

  const sorted = [...items].sort((a, b) =>
    b.createdAt.localeCompare(a.createdAt),
  );
  const visible = sorted.filter((item) => matchesQuery(item, query));

  const newButton = (
    <Button asChild size="sm">
      <Link href={newHref}>
        <Plus aria-hidden />
        {newLabel}
      </Link>
    </Button>
  );

  return (
    <>
      <PageHeader
        title={title}
        meta={
          isPending || isError || items.length === 0
            ? undefined
            : `${items.length.toLocaleString()} ${items.length === 1 ? noun.replace(/s$/, "") : noun}`
        }
        actions={items.length > 0 ? newButton : undefined}
      />

      <PageBody className="max-w-[760px]">
        {isPending && (
          <div className="space-y-3" aria-hidden>
            <Skeleton className="h-8 w-full" />
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-4 w-full" />
            ))}
          </div>
        )}

        {isError && (
          <p
            role="alert"
            className="border-l-2 border-destructive py-1 pl-3 text-[13px] text-ink-secondary"
          >
            Couldn&rsquo;t load your {noun}. Check that the API is running, then
            reload.
          </p>
        )}

        {!isPending && !isError && items.length === 0 && (
          <div className="py-14 text-center sm:py-20">
            <h2 className="display display-lg">{emptyHeadline}</h2>
            <p className="mx-auto mt-3 max-w-[44ch] text-[14px] text-ink-secondary">
              {emptyBody}
            </p>
            <div className="mt-6">{newButton}</div>
          </div>
        )}

        {!isPending && !isError && items.length > 0 && (
          <>
            <div className="relative">
              <Search
                aria-hidden
                className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-ink-tertiary"
              />
              <Input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={`Search ${noun}`}
                aria-label={`Search ${noun}`}
                className="h-9 bg-card pl-8"
              />
            </div>

            {visible.length === 0 ? (
              <p className="mt-6 text-[13px] text-ink-secondary">
                No {noun} match &ldquo;{query.trim()}&rdquo;.
              </p>
            ) : (
              <div className="mt-2">
                {groupByDay(visible).map(([day, group]) => (
                  <section key={day}>
                    <h2 className="label-caps pt-6 pb-2 text-ink-tertiary">
                      {day}
                    </h2>
                    <ul className="border-t border-border">
                      {group.map((item) => (
                        <li key={item.id} className="border-b border-border">
                          <Link
                            href={item.href}
                            className="-mx-2 flex items-baseline gap-3 rounded-[4px] px-2 py-2.5 text-[14px] transition-colors duration-[120ms] hover:bg-accent/60"
                          >
                            <span className="cite w-10 shrink-0 text-ink-tertiary">
                              #{item.id}
                            </span>
                            <span className="min-w-0 flex-1 truncate text-ink-primary">
                              {item.title}
                            </span>
                            <time
                              dateTime={item.createdAt}
                              className="shrink-0 text-[12px] text-ink-tertiary"
                            >
                              {formatRelativeTime(item.createdAt)}
                            </time>
                          </Link>
                        </li>
                      ))}
                    </ul>
                  </section>
                ))}
              </div>
            )}
          </>
        )}
      </PageBody>
    </>
  );
}
