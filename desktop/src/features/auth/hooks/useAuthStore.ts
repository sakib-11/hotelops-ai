import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import type { SessionState, UserInfo, AuthTokens, AuthStatus } from "../types";
import { createAuthService } from "../services/authStub";
import { queryClient } from "@/query/client";
import { webSocketManager } from "@/realtime/WebSocketManager";

interface AuthStore extends SessionState {
  // Actions
  initialize: () => Promise<void>;
  login: (credentials: { email: string; password: string }) => Promise<void>;
  logout: () => Promise<void>;
  refreshSession: () => Promise<void>;
  clearError: () => void;
  setStatus: (status: AuthStatus) => void;
  setUser: (user: UserInfo | null) => void;
  setTokens: (tokens: AuthTokens | null) => void;
}

const authService = createAuthService();

export const useAuthStore = create<AuthStore>()(
  persist(
    (set, get) => ({
      // Initial state
      status: "initializing",
      user: null,
      tokens: null,
      error: null,
      isRestoring: false,

      // Initialize auth state on app start
      initialize: async () => {
        set({ status: "initializing", isRestoring: true, error: null });

        try {
          const tokens = get().tokens;
          if (!tokens?.access_token) {
            set({ status: "unauthenticated", isRestoring: false });
            return;
          }

          // Try to restore session by getting current user
          const user = await authService.getCurrentUser();
          set({
            status: "authenticated",
            user,
            tokens: get().tokens,
            isRestoring: false,
            error: null,
          });

          // Connect realtime WebSocket
          webSocketManager.connect(tokens.access_token);
        } catch {
          // Session invalid or expired - clear all sensitive state
          authService.clearTokens?.();
          queryClient.clear();
          webSocketManager.disconnect();

          set({
            status: "unauthenticated",
            user: null,
            tokens: null,
            isRestoring: false,
            error: null,
          });
        }
      },

      // Login with credentials
      login: async (credentials) => {
        set({ status: "authenticating", error: null });

        try {
          const response = await authService.login(credentials);

          set({
            status: "authenticated",
            user: response.user,
            tokens: response.tokens,
            error: null,
          });

          // Connect realtime WebSocket with new token
          webSocketManager.connect(response.tokens.access_token);
        } catch (error) {
          if (error instanceof Error && "code" in error) {
            const authError = error as { code: string; message: string; status?: number };
            set({
              status: "auth_error",
              error: {
                code: authError.code as
                  | "invalid_credentials"
                  | "network_error"
                  | "server_error"
                  | "session_expired"
                  | "permission_denied"
                  | "unknown",
                message: authError.message,
                status: authError.status,
              },
            });
          } else {
            set({
              status: "auth_error",
              error: {
                code: "unknown",
                message: "An unexpected error occurred. Please try again.",
              },
            });
          }
          throw error;
        }
      },

      // Logout - deterministic cleanup of all session, cache, and WebSocket state
      logout: async () => {
        set({ status: "authenticating" });

        try {
          await authService.logout();
        } catch {
          // Ignore server logout errors - clear local state anyway
        } finally {
          // Purge query cache to ensure complete user isolation
          queryClient.clear();

          // Disconnect WebSocket and cancel reconnect timers
          webSocketManager.disconnect();

          set({
            status: "unauthenticated",
            user: null,
            tokens: null,
            error: null,
          });
        }
      },

      // Refresh session
      refreshSession: async () => {
        const { tokens } = get();
        if (!tokens?.access_token) {
          set({ status: "unauthenticated" });
          return;
        }

        try {
          const user = await authService.getCurrentUser();
          set({ status: "authenticated", user, error: null });
        } catch {
          // Session expired
          queryClient.clear();
          webSocketManager.disconnect();

          set({
            status: "session_expired",
            user: null,
            tokens: null,
            error: {
              code: "session_expired",
              message: "Your session has expired. Please sign in again.",
            },
          });
        }
      },

      // Clear error state
      clearError: () => set({ error: null }),

      // Direct state setters
      setStatus: (status) => set({ status }),
      setUser: (user) => set({ user }),
      setTokens: (tokens) => set({ tokens }),
    }),
    {
      name: "hotelops-auth",
      storage: createJSONStorage(() => sessionStorage),
      partialize: (state) => ({
        tokens: state.tokens,
        user: state.user,
      }),
    },
  ),
);

// Selectors
export const useAuthStatus = () => useAuthStore((state) => state.status);
export const useAuthUser = () => useAuthStore((state) => state.user);
export const useAuthTokens = () => useAuthStore((state) => state.tokens);
export const useAuthError = () => useAuthStore((state) => state.error);
export const useAuthIsRestoring = () => useAuthStore((state) => state.isRestoring);
export const useIsAuthenticated = () => useAuthStore((state) => state.status === "authenticated");
export const useIsInitializing = () =>
  useAuthStore((state) => state.status === "initializing" || state.isRestoring);

// Action hooks
export const useAuthActions = () =>
  useAuthStore((state) => ({
    initialize: state.initialize,
    login: state.login,
    logout: state.logout,
    refreshSession: state.refreshSession,
    clearError: state.clearError,
  }));
