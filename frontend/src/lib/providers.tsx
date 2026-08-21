"use client";

/**
 * Client-side providers. This is one of the few components that MUST be a
 * client component — TanStack Query holds mutable cache state in React context.
 *
 * The QueryClient is created inside useState so each browser session gets its
 * own instance; a module-level client would be shared across requests on the
 * server and leak one user's cached data into another's response.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { useState, type ReactNode } from "react";

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60_000, // forecasts/schedules do not change second-to-second
            retry: 1,
            refetchOnWindowFocus: false,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      {children}
      <ReactQueryDevtools initialIsOpen={false} />
    </QueryClientProvider>
  );
}
