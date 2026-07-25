"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";

import type { CapturedQuery } from "@/lib/api/types";

interface SourcesValue {
  queries: CapturedQuery[];
  /** The dataset currently hovered or pinned, whichever is more specific. */
  active: string | null;
  pinned: string | null;
  hover: (datasetId: string | null) => void;
  pin: (datasetId: string) => void;
}

const SourcesContext = createContext<SourcesValue | null>(null);

/**
 * Ties the citation chips scattered through a document to the query list in
 * the Sources rail. Optional by design: a document rendered outside a provider
 * (a Q&A answer inline, a fixture in a test) still renders, just without the
 * cross-highlight.
 */
export function SourcesProvider({
  queries,
  children,
}: {
  queries: CapturedQuery[];
  children: React.ReactNode;
}) {
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);

  const pin = useCallback(
    (datasetId: string) =>
      setPinned((current) => (current === datasetId ? null : datasetId)),
    [],
  );

  const value = useMemo<SourcesValue>(
    () => ({
      queries,
      active: hovered ?? pinned,
      pinned,
      hover: setHovered,
      pin,
    }),
    [queries, hovered, pinned, pin],
  );

  return (
    <SourcesContext.Provider value={value}>{children}</SourcesContext.Provider>
  );
}

export function useSources() {
  return useContext(SourcesContext);
}
