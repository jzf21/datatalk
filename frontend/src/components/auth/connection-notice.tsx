"use client";

import { useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { useSession } from "./session-gate";

const SETTINGS_PATH = "/settings/connection";

/**
 * The other half of the API's 409 `no_connection`.
 *
 * A workspace with no ClickHouse connection can still browse its library, so
 * it does not get bounced to sign-in -- but every warehouse-backed call will
 * fail until someone configures one. Two things happen here:
 *
 * - any 409 announced by the API client sends the user to the settings panel,
 *   which is the only screen that can resolve it;
 * - a fresh workspace gets a standing banner, so the first report attempt
 *   isn't how they find out.
 */
export function ConnectionNotice() {
  const { connectionConfigured } = useSession();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    const onNoConnection = () => {
      if (window.location.pathname !== SETTINGS_PATH) router.push(SETTINGS_PATH);
    };
    window.addEventListener("datatalk:no-connection", onNoConnection);
    return () =>
      window.removeEventListener("datatalk:no-connection", onNoConnection);
  }, [router]);

  // Nothing to say once it is configured, or while they are already fixing it.
  if (connectionConfigured || pathname.startsWith(SETTINGS_PATH)) return null;

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border bg-accent/60 px-6 py-2 text-[12px] text-ink-secondary"
    >
      <span className="text-ink-primary">No warehouse connected.</span>
      <span>Reports and questions need a ClickHouse connection.</span>
      <Link
        href={SETTINGS_PATH}
        className="font-medium text-ink-primary underline underline-offset-2"
      >
        Connect one
      </Link>
    </div>
  );
}
