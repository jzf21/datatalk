"use client";

import { createContext, useContext, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { getMe, type AuthOrg, type AuthUser, type MeResponse } from "@/lib/api/auth";
import { AuthScreen } from "./auth-screen";

interface SessionValue {
  user: AuthUser;
  org: AuthOrg | null;
  /** Every org the user belongs to -- drives the switcher in the account menu. */
  orgs: AuthOrg[];
  connectionConfigured: boolean;
}

const SessionContext = createContext<SessionValue | null>(null);

export const meQueryKey = ["me"] as const;

/**
 * Everything behind the app shell requires a session.
 *
 * `/api/auth/me` answers 200 `{authenticated:false}` rather than 401, so the
 * boot path has no error state to special-case -- only "signed in" or not.
 */
export function SessionGate({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const { data, isPending, isError, error } = useQuery<MeResponse>({
    queryKey: meQueryKey,
    queryFn: ({ signal }) => getMe(signal),
    retry: false,
    staleTime: 60_000,
  });

  // A session can expire while the tab is open. `client.ts` announces any 401
  // centrally; re-reading /me then drops us to the sign-in screen instead of
  // leaving a shell full of failed queries.
  useEffect(() => {
    const onUnauthorized = () =>
      qc.invalidateQueries({ queryKey: meQueryKey });
    window.addEventListener("datatalk:unauthorized", onUnauthorized);
    return () =>
      window.removeEventListener("datatalk:unauthorized", onUnauthorized);
  }, [qc]);

  if (isPending) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-[13px] text-ink-tertiary">Loading…</p>
      </div>
    );
  }

  // Unreachable API is a different failure from being signed out, and saying
  // so saves the "why does login not work" hunt.
  if (isError) {
    return (
      <div className="flex min-h-screen items-center justify-center p-8">
        <div className="max-w-md space-y-2 text-center">
          <h1 className="text-[17px] font-semibold text-ink-primary">
            Can&rsquo;t reach the DataTalk API
          </h1>
          <p className="text-[13px] text-ink-secondary">{String(error)}</p>
        </div>
      </div>
    );
  }

  if (!data.authenticated) return <AuthScreen />;

  return (
    <SessionContext.Provider
      value={{
        user: data.user,
        org: data.org,
        orgs: data.orgs,
        connectionConfigured: data.connection?.configured ?? false,
      }}
    >
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside SessionGate");
  return value;
}

/** Optional variant for components that may render outside a session. */
export function useMaybeSession() {
  return useContext(SessionContext);
}

export function useInvalidateSession() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: meQueryKey });
}
