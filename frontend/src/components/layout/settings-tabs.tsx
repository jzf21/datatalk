"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

export const SETTINGS_TABS = [
  { href: "/settings/connection", label: "Data sources" },
  { href: "/settings/scope", label: "Data scope" },
  { href: "/settings/context", label: "Data context" },
  { href: "/settings/rules", label: "House rules" },
] as const;

/**
 * One Settings entry in the sidebar, these tabs inside it. Everything here
 * shapes what the agent sees or how it behaves, so it lives in one place
 * rather than as four peers of Reports and Dashboards.
 */
export function SettingsTabs() {
  const pathname = usePathname();

  return (
    <nav aria-label="Settings" className="-mb-px overflow-x-auto">
      <ul className="flex gap-5">
        {SETTINGS_TABS.map(({ href, label }) => {
          const active = pathname === href || pathname.startsWith(`${href}/`);
          return (
            <li key={href} className="shrink-0">
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "block border-b-2 pt-1 pb-2 text-[13px] transition-colors duration-[120ms]",
                  active
                    ? "border-ink-primary font-medium text-ink-primary"
                    : "border-transparent text-ink-secondary hover:text-ink-primary",
                )}
              >
                {label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
