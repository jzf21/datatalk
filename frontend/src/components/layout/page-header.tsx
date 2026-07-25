import { cn } from "@/lib/utils";

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
  return (
    <header
      className={cn(
        "sticky top-0 z-20 flex min-h-14 items-center gap-4 border-b border-border bg-background/95 px-6 backdrop-blur",
        className,
      )}
    >
      <div className="min-w-0 flex-1 py-2">
        <h1 className="truncate text-[15px] font-semibold tracking-[-0.012em] text-ink-primary">
          {title}
        </h1>
        {meta && <p className="mt-0.5 truncate text-[12px] text-ink-secondary">{meta}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
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
