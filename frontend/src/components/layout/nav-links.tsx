"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  FileText,
  LayoutDashboard,
  Settings,
  Sparkles,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

/** `section` is the path prefix that marks the link active, when it differs
 *  from where the link goes. */
const LINKS: {
  href: string;
  label: string;
  icon: LucideIcon;
  section?: string;
}[] = [
  { href: "/reports", label: "Reports", icon: FileText },
  { href: "/dashboards", label: "Dashboards", icon: LayoutDashboard },
  { href: "/analyze", label: "Analyze", icon: Sparkles },
  {
    href: "/settings/connection",
    label: "Settings",
    icon: Settings,
    section: "/settings",
  },
];

export function NavLinks() {
  const pathname = usePathname();

  return (
    <nav className="px-2" aria-label="Main">
      <ul className="space-y-px">
        {LINKS.map(({ href, label, icon: Icon, section = href }) => {
          const active =
            pathname === section || pathname.startsWith(`${section}/`);
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
