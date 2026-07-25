"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Check } from "lucide-react";

import { logout, switchOrgAndReload } from "@/lib/api/auth";
import { useMaybeSession } from "@/components/auth/session-gate";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

export function AccountMenu() {
  const session = useMaybeSession();
  const qc = useQueryClient();
  const [switching, setSwitching] = useState(false);

  if (!session) return null;

  const { user, org, orgs } = session;
  // The switcher only earns its place once there is somewhere to switch to.
  const showSwitcher = orgs.length > 1;

  const switchTo = async (orgId: string) => {
    if (orgId === org?.id || switching) return;
    setSwitching(true);
    // No finally: on success the page hard-reloads out from under us, so
    // clearing the flag only matters on the (rare) failure path.
    try {
      await switchOrgAndReload(orgId);
    } catch {
      setSwitching(false);
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="flex w-full items-center gap-2 rounded-[4px] px-1 py-1 text-left text-[12px] text-ink-secondary hover:bg-accent hover:text-ink-primary">
        <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-accent text-[10px] font-medium text-ink-secondary">
          {user.email.slice(0, 1).toUpperCase()}
        </span>
        <span className="truncate">{org?.name ?? user.email}</span>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="start" side="top" className="w-56">
        <DropdownMenuLabel className="font-normal">
          <p className="truncate text-[13px] text-ink-primary">{user.email}</p>
          {org && (
            <p className="text-[12px] text-ink-tertiary">
              {org.name} · {org.role}
            </p>
          )}
        </DropdownMenuLabel>

        {showSwitcher && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuLabel className="label-caps text-ink-tertiary">
              Workspaces
            </DropdownMenuLabel>
            {orgs.map((o) => {
              const current = o.id === org?.id;
              return (
                <DropdownMenuItem
                  key={o.id}
                  disabled={switching}
                  // A tenant switch must be a full document load, never a state
                  // swap -- so onSelect kicks off a hard reload, not navigation.
                  onSelect={(e) => {
                    e.preventDefault();
                    void switchTo(o.id);
                  }}
                >
                  <Check
                    className={`size-3.5 ${current ? "opacity-100" : "opacity-0"}`}
                    aria-hidden
                  />
                  <span className="truncate">{o.name}</span>
                </DropdownMenuItem>
              );
            })}
          </>
        )}

        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={async () => {
            await logout();
            // Drop every cached org-scoped query, not just the session.
            qc.clear();
          }}
        >
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
