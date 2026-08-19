/**
 * Query Provider - React context provider for TanStack Query.
 *
 * Wraps the application with the QueryClientProvider and optionally
 * React Query Devtools (development only).
 *
 * This provider must wrap the entire application tree that uses
 * server-state cache hooks.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { queryClient } from "./client";
import { useAuthStore } from "@/features/auth/hooks/useAuthStore";
import { clearAuthQueries } from "./hooks/useAuth";
import { useEffect } from "react";

interface QueryProviderProps {
  children: React.ReactNode;
}

/**
 * QueryProvider component.
 *
 * Wraps children with QueryClientProvider.
 * In development, also includes React Query Devtools.
 *
 * Listens for logout events to clear user-specific cache.
 */
export function QueryProvider({ children }: QueryProviderProps) {
  useEffect(() => {
    let prevStatus = useAuthStore.getState().status;
    const unsubscribe = useAuthStore.subscribe((state) => {
      if (state.status === "unauthenticated" && prevStatus !== "unauthenticated") {
        clearAuthQueries();
      }
      prevStatus = state.status;
    });

    return unsubscribe;
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      {children}
      {import.meta.env.DEV && <ReactQueryDevtools initialIsOpen={false} />}
    </QueryClientProvider>
  );
}

export { queryClient } from "./client";
