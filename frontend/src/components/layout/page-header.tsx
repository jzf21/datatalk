"use client";

import { createContext, useContext } from "react";

import { cn } from "@/lib/utils";

/**
 * Secondary navigation a layout hands to every page beneath it (the Settings
 * tabs). It renders inside the page's own sticky header, so a section's pages
 * keep their titles and the tabs stay in reach while scrolling.
 */
export const PageSubnavContext = createContext<React.ReactNode>(null);

/**
 * The sticky 56px bar at the top of every page. `meta` carries the run summary
 * line; `actions` the trailing controls.
 */
export function PageHeader({
  title,
  meta,
  actions,
  className,
}: {
  title: React.ReactNode;
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  const subnav = useContext(PageSubnavContext);

  return (
    <header
      className={cn(
        "sticky top-0 z-20 border-b border-border bg-background/95 px-6 backdrop-blur",
        className,
      )}
    >
      <div className="flex min-h-14 items-center gap-4">
        <div className="min-w-0 flex-1 py-2">
          <h1 className="truncate text-[15px] font-semibold tracking-[-0.012em] text-ink-primary">
            {title}
          </h1>
          {meta && <p className="mt-0.5 truncate text-[12px] text-ink-secondary">{meta}</p>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      {subnav}
    </header>
  );
}

/** The 1040px content column every page shares. */
export function PageBody({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <main className={cn("mx-auto w-full max-w-[1040px] px-6 py-8", className)}>
      {children}
    </main>
  );
}
