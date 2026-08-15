"use client";

import { useEffect, useState } from "react";

/**
 * A slow clock, for relative timestamps that must not go stale on screen.
 *
 * `formatRelativeTime` reads `Date.now()` once at render, so an "Updated 5s ago"
 * line freezes at whatever it said when the component last rendered -- which on
 * a dashboard that is only re-rendered by a refresh means it lies for exactly as
 * long as nothing is happening. Ticking re-reads it.
 *
 * Also re-reads on `visibilitychange`: a tab left for an hour should not come
 * back still claiming "just now" until its next tick.
 */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const tick = () => setNow(Date.now());
    const id = window.setInterval(tick, intervalMs);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [intervalMs]);

  return now;
}
