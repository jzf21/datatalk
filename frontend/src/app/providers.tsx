"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";

export function Providers({ children }: { children: React.ReactNode }) {
  // useState, not a module-level client: one QueryClient per browser session,
  // never shared across requests.
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            retry: 1,
            refetchOnWindowFocus: false,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={client}>
      <TooltipProvider delayDuration={200}>{children}</TooltipProvider>
      {/* Transient confirmations only -- copied, deleted, undo. Never a
          generation error: those belong where the work is. */}
      <Toaster position="bottom-right" />
    </QueryClientProvider>
  );
}
