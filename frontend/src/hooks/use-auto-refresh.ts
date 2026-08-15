"use client";

import { useCallback, useSyncExternalStore } from "react";

export interface RefreshOption {
  ms: number | null;
  label: string;
}

/** Off first: a dashboard should not start costing warehouse time unasked. */
export const REFRESH_OPTIONS: RefreshOption[] = [
  { ms: null, label: "Off" },
  { ms: 30_000, label: "30s" },
  { ms: 60_000, label: "1m" },
  { ms: 300_000, label: "5m" },
  { ms: 900_000, label: "15m" },
];

const ALLOWED = new Set(
  REFRESH_OPTIONS.map((o) => o.ms).filter((m): m is number => m !== null),
);

export function parseInterval(raw: string | null): number | null {
  if (!raw) return null;
  const n = Number(raw);
  // Clamp to the offered set rather than trusting localStorage: a hand-edited
  // "1" would otherwise poll a warehouse a thousand times a second.
  return Number.isFinite(n) && ALLOWED.has(n) ? n : null;
}

export function storageKey(dashboardId: number): string {
  return `datatalk:auto-refresh:${dashboardId}`;
}

export function labelFor(ms: number | null): string {
  return REFRESH_OPTIONS.find((o) => o.ms === ms)?.label ?? "Off";
}

// localStorage is external state, so it is read through a store subscription
// rather than copied into React state by an effect. That keeps the server
// snapshot (`null`, i.e. off) honest during hydration without a cascading
// render, and it also means two tabs on the same dashboard stay in step, since
// `storage` fires in the tab that did not make the change.
const listeners = new Set<() => void>();

function notify() {
  for (const listener of listeners) listener();
}

function subscribe(onChange: () => void) {
  listeners.add(onChange);
  window.addEventListener("storage", onChange);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onChange);
  };
}

function read(dashboardId: number | null): string | null {
  if (dashboardId === null) return null;
  try {
    return window.localStorage.getItem(storageKey(dashboardId));
  } catch {
    // Private mode, or storage disabled. Off is a fine answer.
    return null;
  }
}

/**
 * The auto-refresh interval for one dashboard, remembered per browser.
 *
 * Per user rather than per dashboard row: an interval is a viewing preference,
 * and making it a shared property of the dashboard would let one person's choice
 * spend everyone else's warehouse budget.
 */
export function useAutoRefresh(dashboardId: number | null) {
  const raw = useSyncExternalStore(
    subscribe,
    () => read(dashboardId),
    () => null, // server: always off, so first paint never disagrees
  );

  const choose = useCallback(
    (next: number | null) => {
      if (dashboardId === null) return;
      try {
        if (next === null) window.localStorage.removeItem(storageKey(dashboardId));
        else window.localStorage.setItem(storageKey(dashboardId), String(next));
      } catch {
        // Non-fatal, but then there is nothing to notify about either.
      }
      notify();
    },
    [dashboardId],
  );

  return { ms: parseInterval(raw), setMs: choose };
}
