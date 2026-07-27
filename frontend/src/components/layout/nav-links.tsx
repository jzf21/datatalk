"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BookOpen,
  FileText,
  LayoutDashboard,
  ListFilter,
  Sparkles,
  ScrollText,
  Cable,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

const LINKS: { href: string; label: string; icon: LucideIcon }[] = [
  { href: "/reports", label: "Reports", icon: FileText },
  { href: "/dashboards", label: "Dashboards", icon: LayoutDashboard },
  { href: "/analyze", label: "Analyze", icon: Sparkles },
  { href: "/memory", label: "House rules", icon: ScrollText },
  { href: "/settings/connection", label: "Connection", icon: Cable },
  { href: "/settings/scope", label: "Data scope", icon: ListFilter },
  { href: "/settings/context", label: "Data context", icon: BookOpen },
];

export function NavLinks() {
  const pathname = usePathname();

  return (
    <nav className="px-2" aria-label="Main">
      <ul className="space-y-px">
        {LINKS.map(({ href, label, icon: Icon }) => {
          const active = pathname === href || pathname.startsWith(`${href}/`);
          return (
            <li key={href}>
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-2.5 rounded-md px-2 py-1.5 text-[14px] transition-colors duration-[120ms]",
                  active
                    ? "bg-accent font-medium text-ink-primary"
                    : "text-ink-secondary hover:bg-accent/60 hover:text-ink-primary",
                )}
              >
                <Icon
                  aria-hidden
                  className={cn(
                    "size-4 shrink-0",
                    active ? "text-ink-secondary" : "text-ink-tertiary",
                  )}
                />
                {label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
