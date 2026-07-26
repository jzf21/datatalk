import Link from "next/link";

import { ConnectionNotice } from "@/components/auth/connection-notice";
import { NavLinks } from "./nav-links";
import { LibraryRail } from "./library-rail";
import { HealthPill } from "./health-pill";
import { AccountMenu } from "./account-menu";

/**
 * Rail + content.
 *
 * The rail sits on the *same* background as the canvas, separated by a single
 * hairline -- deliberately not a "sidebar world / content world" split. Colour
 * is reserved for data, so the chrome is paper and ink all the way across.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen">
      <a
        href="#doc"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-popover focus:px-3 focus:py-2 focus:text-sm focus:shadow-[var(--shadow-overlay)]"
      >
        Skip to document
      </a>

      <aside className="hidden w-[264px] shrink-0 flex-col border-r border-border md:flex">
        <div className="px-4 py-5">
          <Link
            href="/reports"
            className="display text-[22px] tracking-[-0.02em] text-ink-primary"
          >
            DataTalk
          </Link>
          <p className="mt-1 text-[12px] text-ink-tertiary">
            Every number cites its query
          </p>
        </div>

        <NavLinks />
        <LibraryRail />

        <div className="mt-auto space-y-1 border-t border-border px-4 py-3">
          <HealthPill />
          <AccountMenu />
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <ConnectionNotice />
        {children}
      </div>
    </div>
  );
}
