"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/reports", label: "Reports" },
  { href: "/dashboards", label: "Dashboards" },
  { href: "/analyze", label: "Analyze" },
  { href: "/memory", label: "House rules" },
  { href: "/settings/connection", label: "Connection" },
] as const;

export function NavLinks() {
  const pathname = usePathname();

  return (
    <nav className="px-2" aria-label="Main">
      <ul className="space-y-px">
        {LINKS.map(({ href, label }) => {
          const active = pathname === href || pathname.startsWith(`${href}/`);
          return (
            <li key={href}>
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "block rounded-[4px] px-2 py-1.5 text-[14px] transition-colors duration-[120ms]",
                  active
                    ? "bg-accent font-medium text-ink-primary"
                    : "text-ink-secondary hover:bg-accent/60 hover:text-ink-primary",
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
